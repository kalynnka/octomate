"""Thread workspaces: the trees a run happens in, kept as volumes in each user's
root, and their lifecycle from open to save to release."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import overload

import anyio
from pydantic import UUID7

from octomate.config.mirrors import GitIdentity
from octomate.config.workspaces import WorkspacesConfig
from octomate.database import async_session
from octomate.managers.base import Locks, Manager
from octomate.managers.project import ProjectManager
from octomate.managers.workspaces.dependencies import install
from octomate.managers.workspaces.mirrors import (
    GitCommandError,
    MirrorManager,
    run_git,
)
from octomate.managers.workspaces.snapshots import SNAPSHOT_PREFIX, Snapshot
from octomate.managers.workspaces.transfers import copy_ignored
from octomate.schemas.project import Project
from octomate.schemas.thread import Thread
from octomate.schemas.volumes import CacheVolume, MemoryVolume, TreeVolume
from octomate.telemetry import workspace_logfire

logger = logging.getLogger(__name__)


def tree_ref(volume_id: UUID7) -> str:
    """Where a tree's saved work is kept in its project's mirror.

    Outside `refs/heads/`, so the mirror does not grow a branch per tree. Keyed by
    the tree's volume rather than its thread, so two users working in one shared
    thread never save over each other. A fork receives only its own snapshot
    alongside the project's public history.
    """
    return f"refs/octomate/trees/{volume_id}"


def thread_branch(thread_id: uuid.UUID) -> str:
    """The branch a thread's first fork starts on, its own."""
    return f"octomate/thread-{thread_id}"


class UnregisteredProjectError(PermissionError):
    """An unregistered sender asked to work in a project. A project's tree lives in
    a registered user's root, so an unregistered sender may only chat."""


@dataclass
class Workspace(ABC):
    """A run's directory and identity, with its lifetime owned by the manager."""

    path: Path  # Absolute host path, known before the directory is prepared.
    thread_id: UUID7

    @property
    @abstractmethod
    def owner_id(self) -> UUID7 | None:
        """The registered owner, or None for an unregistered chat."""


@dataclass
class TreeWorkspace(Workspace, ABC):
    """A tree volume in a registered user's root: a fork of a mirror, checked out
    on the thread's own branch.

    The volume's row outlives its folder. Releasing a tree deletes the folder and
    keeps the row, so the next fork lands at the same path — which is what lets a
    harness resume a session it keyed by its working directory.
    """

    volume: TreeVolume
    root: Path = field(
        kw_only=True
    )  # The owner's host directory containing all volumes.

    @property
    def owner_id(self) -> UUID7:
        return self.volume.owner_id


@dataclass
class ProjectWorkspace(TreeWorkspace):
    """This user's own checkout of the thread's project.

    A thread runs here rather than in `project.root`, so two threads on one
    project, or two users in one thread, cannot walk over each other's uncommitted
    work — and the root stays the person's, untouched by anything an agent does.
    """

    project: Project


@dataclass
class ChatWorkspace(TreeWorkspace):
    """The tree a registered user's run in no project happens in: a fork of the
    empty repository, its folder gone when the run ends.

    A tree rather than a bare directory, so a run in no project is a run like any
    other — it may write, unpack and script inside its own tree. Forking nothing is
    what makes that affordable: an empty repository costs a `git init`'s worth of
    disk, and binding to a project stops meaning "you may write now" and starts
    meaning "your work is kept now".

    Its row is a tree with no project, kept like any other, so the path is the same
    on every turn even though the folder is not: both runtimes key a resumable
    session by the directory it ran in.
    """

    @property
    def key(self) -> UUID7:
        """What overlapping runs in this folder are counted by."""
        return self.volume.id


@dataclass
class UnregisteredWorkspace(Workspace):
    """Where an unregistered sender's run happens: an empty temporary directory,
    with no git and no volume, gone when the run ends.

    An unregistered sender has no root to hold a volume, so they may only chat.
    The path is the thread's, so a runtime that keys its session by working
    directory can still resume the conversation.
    """

    @property
    def owner_id(self) -> None:
        return None

    @property
    def key(self) -> UUID7:
        """What overlapping runs in this directory are counted by."""
        return self.thread_id


class WorkspaceManager(Manager, Locks[UUID7]):
    """Every run's workspace. A registered user's run gets a tree volume in that
    user's root, ``volumes_dir/<owner>/tree/<volume>``, recorded by a row and
    checked out on the thread's own branch; an unregistered sender's run gets an
    empty temporary directory.

    A fork is a complete, independent repository — its own `.git`, its own object
    store — containing only project history and this tree's saved snapshot.
    The mirror's private refs, unreachable objects and host metadata stay on
    the host; no hardlinks or object alternates connect the two repositories.

    A project's tree is a cache rather than the only copy: each turn's tree is
    snapshotted and pushed to the mirror under the tree's own ref, so releasing
    one is a disk decision and resuming lays that ref back over a fresh fork.
    Releasing deletes the folder and keeps the row. What the agent did with git in
    there is the agent's — nothing here commits.

    A registered user's thread in no project gets a tree too, forked from the blank
    mirror and thrown away when the run that made it ends. Same mechanism, opposite
    ending: there is no project to keep it for.

    A turn enters `open` to prepare its workspace and scope its lifetime.
    Project exit checkpoints its exact volume; chat exit releases its temporary
    directory. `resolve` records a volume and settles its path without preparing it.
    Explicit `save` and `dismiss` resolve an owner's existing tree for
    graph finalization and handoffs. The registry identifies the project and the
    mirrors hold its checkpoints.
    """

    def __init__(
        self,
        *,
        projects: ProjectManager | None = None,
        mirrors: MirrorManager | None = None,
        config: WorkspacesConfig | None = None,
        volumes_dir: Path = Path(".octomate/volumes"),
    ) -> None:
        # A workspace is a project-bound thread's, so the registry that says which
        # project and the mirrors that say where are what this works through. Its
        # host replaces both with its own when it takes ownership — a second
        # registry would resolve no thread to a project, and a second mirror
        # manager would keep its own sync locks — so a manager built without them
        # is one nobody has handed to a host yet.
        self.projects = projects if projects is not None else ProjectManager()
        self.mirrors = mirrors if mirrors is not None else MirrorManager()
        self.config = config if config is not None else WorkspacesConfig()
        # Resolved once, here: a volume path leaves this manager to become a
        # subprocess's cwd and a `GIT_INDEX_FILE` beside it, and the default is
        # relative to wherever Octomate was started.
        self.volumes_dir = volumes_dir.resolve()
        # How many runs are in each throwaway directory right now, keyed by the
        # chat tree's volume or the unregistered thread. Two overlapping runs of
        # one conversation share it — this is what stops the first to finish
        # taking it from the second.
        self.chatting: dict[UUID7, int] = {}

    @property
    def identity(self) -> GitIdentity:
        """Who the machine is when it writes a snapshot — the `mirrors:` block's,
        read rather than copied, so a turn's work and a folder sync into a mirror
        carry one name and cannot drift into two."""
        return self.mirrors.config.identity

    def root(self, owner_id: UUID7) -> Path:
        """The one host directory a habitat may attach for this owner.

        Resolving it creates neither files nor database rows.
        """
        return self.volumes_dir / str(owner_id)

    def path(self, volume: TreeVolume | MemoryVolume | CacheVolume) -> Path:
        """Where this volume's folder lives in its owner's root. Derived from the
        row's identity, never stored, so the store can change without a
        migration."""
        return self.root(volume.owner_id) / volume.kind / str(volume.id)

    async def find(
        self, owner_id: UUID7, thread_id: UUID7, project_id: UUID7 | None
    ) -> TreeVolume | None:
        """This owner's tree for this thread and project, if one was ever opened."""
        async with async_session() as session:
            return await session.one_or_none(
                TreeVolume,
                expressions=[
                    TreeVolume["owner_id"] == owner_id,
                    TreeVolume["thread_id"] == thread_id,
                    TreeVolume["project_id"].is_(None)
                    if project_id is None
                    else TreeVolume["project_id"] == project_id,
                ],
            )

    async def tree(
        self,
        owner_id: UUID7,
        thread_id: UUID7,
        project_id: UUID7 | None,
        ref: str | None = None,
    ) -> TreeVolume:
        """This owner's tree for this thread and project, its row written now when
        there is none, and stamped as used either way.

        The row comes first and the folder later, when the run enters the
        workspace. `ref` is where a first fork starts, kept on the row; a tree that
        already has a row keeps the ref it was opened with.
        """
        async with self.lock(thread_id):
            volume = await self.find(owner_id, thread_id, project_id)
            async with async_session() as session:
                if volume is None:
                    volume = TreeVolume(
                        owner_id=owner_id,
                        thread_id=thread_id,
                        project_id=project_id,
                        base_ref=ref,
                    )
                else:
                    volume.last_used_at = datetime.now(UTC)
                session.add(volume)
                await session.commit()
            return volume

    @overload
    async def resolve(
        self,
        thread_id: UUID7,
        project: Project,
        owner_id: UUID7,
        ref: str | None = None,
    ) -> ProjectWorkspace: ...

    @overload
    async def resolve(
        self,
        thread_id: UUID7,
        project: None,
        owner_id: UUID7,
        ref: None = None,
    ) -> ChatWorkspace: ...

    @overload
    async def resolve(
        self,
        thread_id: UUID7,
        project: Project | None,
        owner_id: UUID7 | None,
        ref: str | None = None,
    ) -> Workspace: ...

    async def resolve(
        self,
        thread_id: UUID7,
        project: Project | None,
        owner_id: UUID7 | None,
        ref: str | None = None,
    ) -> Workspace:
        """Record the owner's volume and settle its path without making files.

        Unregistered senders receive a temporary chat path and cannot resolve
        project workspaces. This writes or stamps a registered volume row; use
        `find` and `path` for read-only inspection.
        """
        if owner_id is None:
            if project is not None:
                raise UnregisteredProjectError(
                    f"project {project.name!r} needs a registered user"
                )
            path = (
                Path(tempfile.gettempdir()) / "octomate-unregistered" / str(thread_id)
            ).resolve()
            return UnregisteredWorkspace(path, thread_id)
        if project is None:
            volume = await self.tree(owner_id, thread_id, None)
            return ChatWorkspace(
                self.path(volume),
                thread_id,
                volume,
                root=self.root(owner_id),
            )
        volume = await self.tree(owner_id, thread_id, project.id, ref)
        return ProjectWorkspace(
            self.path(volume),
            thread_id,
            volume,
            project,
            root=self.root(owner_id),
        )

    @overload
    def open(
        self,
        thread_id: UUID7,
        project: Project,
        owner_id: UUID7,
        ref: str | None = None,
    ) -> AbstractAsyncContextManager[ProjectWorkspace]: ...

    @overload
    def open(
        self,
        thread_id: UUID7,
        project: None,
        owner_id: UUID7,
        ref: None = None,
    ) -> AbstractAsyncContextManager[ChatWorkspace]: ...

    @overload
    def open(
        self,
        thread_id: UUID7,
        project: Project | None,
        owner_id: UUID7 | None,
        ref: str | None = None,
    ) -> AbstractAsyncContextManager[Workspace]: ...

    @asynccontextmanager
    async def open(
        self,
        thread_id: UUID7,
        project: Project | None,
        owner_id: UUID7 | None,
        ref: str | None = None,
    ) -> AsyncGenerator[Workspace]:
        """Prepare a workspace and own its checkpoint or release when the run ends."""
        workspace = await self.resolve(thread_id, project, owner_id, ref)
        async with self.enter(workspace):
            yield workspace

    @asynccontextmanager
    async def enter[WorkspaceT: Workspace](
        self, workspace: WorkspaceT
    ) -> AsyncGenerator[WorkspaceT]:
        """Scope a resolved workspace, keeping project work on cancellation."""
        await self.prepare(workspace)
        save_project = True
        try:
            yield workspace
        except BaseException as error:
            save_project = isinstance(error, Exception)
            raise
        finally:
            if isinstance(workspace, ProjectWorkspace) and save_project:
                await self.checkpoint(workspace)
            elif isinstance(workspace, ChatWorkspace | UnregisteredWorkspace):
                with anyio.CancelScope(shield=True):
                    await self.discard(workspace)

    async def prepare(self, workspace: Workspace) -> Path:
        """Make a resolved directory ready; a chat acquires a claim until discard."""
        if isinstance(workspace, ProjectWorkspace):
            if workspace.path.is_dir():
                return workspace.path
            mirror = await self.mirrors.sync(workspace.project)
            return await self.materialize(workspace, mirror)
        if not isinstance(workspace, ChatWorkspace | UnregisteredWorkspace):
            raise TypeError(f"Unsupported workspace: {type(workspace).__name__}")
        await self.claim(workspace)
        try:
            if isinstance(workspace, ChatWorkspace):
                mirror = await self.mirrors.create()
                return await self.materialize(workspace, mirror)
            workspace.path.mkdir(parents=True, exist_ok=True)
            return workspace.path
        except BaseException:
            with anyio.CancelScope(shield=True):
                await self.discard(workspace)
            raise

    async def claim(self, workspace: ChatWorkspace | UnregisteredWorkspace) -> None:
        """Count this run into a throwaway directory, and clear whatever the last
        one left.

        Counted, because a mid-run follow-up starts its turn before the turn it
        supersedes has finished losing its own: both runs are in this directory,
        and the first to leave must not take the floor out from under the second.

        A directory nobody is in belongs to a run that is over — one that was
        killed before it could discard — so it goes, and this run makes its own.
        """
        # `holders` is the count after this run joined, and the number to read a
        # throwaway directory's life by: it is what a `discard` has to bring back
        # to zero.
        key = workspace.key
        with workspace_logfire.span("workspace.claim", key=str(key)) as span:
            async with self.lock(key):
                holders = self.chatting.get(key, 0)
                if holders == 0 and workspace.path.exists():
                    # What a run killed before it could discard left behind.
                    span.set_attribute("abandoned_tree", True)
                    await asyncio.to_thread(shutil.rmtree, workspace.path)
                self.chatting[key] = holders + 1
                span.set_attribute("holders", holders + 1)

    async def discard(self, workspace: ChatWorkspace | UnregisteredWorkspace) -> None:
        """Give this throwaway directory back to the disk, once no run is still in
        it."""
        key, path = workspace.key, workspace.path
        with workspace_logfire.span("workspace.discard", key=str(key)) as span:
            async with self.lock(key):
                holders = self.chatting.get(key, 0) - 1
                span.set_attribute("holders", max(holders, 0))
                if holders > 0:
                    # An overlapping run of the same conversation is still in this
                    # directory, so it stays and its life is that run's to end.
                    span.set_attribute("removed", False)
                    self.chatting[key] = holders
                    return
                self.chatting.pop(key, None)
                span.set_attribute("removed", path.is_dir())
                if path.is_dir():
                    await asyncio.to_thread(shutil.rmtree, path)

    async def materialize(self, workspace: TreeWorkspace, mirror: Path) -> Path:
        """Fork `mirror` into `workspace`, and answer the tree.

        The one place a workspace is made, whichever kind it is: a project's
        checkout and a chat thread's empty tree are the same fork of the same
        shape, differing only in what they were forked from. Both land on the
        thread's own branch, and neither carries the mirror's `origin` unless the
        mirror has a real one to inherit.

        A workspace that already exists is answered as it stands — a project
        thread's live tree, uncommitted work included, where forking again would
        throw that work away, and for a chat thread the tree an overlapping run of
        the same conversation is already in. Serialized per thread, so two turns
        arriving together materialize once.

        The fork is made beside the workspace and moved into place, so a workspace
        only ever exists whole: a rename within one directory is atomic, where a
        copy the machine dies in the middle of would leave half a tree that every
        later turn reads as this thread's own. That is what makes "the directory is
        there" mean "the workspace is there", here and for a caller that asks the
        same question. A fork that merely fails is removed the same way, before the
        error travels on. Serialized per tree, so two users forking into one
        thread do not wait on each other.

        It is installed before it is moved into place. Only selected Git objects
        cross from the mirror; host metadata and untracked files do not.
        """
        path = workspace.path
        # Outside the lock, so a turn waiting on another turn's fork is time this
        # span accounts for. From the far end the wait and a slow fork look the
        # same, and they want opposite fixes.
        with workspace_logfire.span(
            "workspace.materialize",
            thread_id=str(workspace.thread_id),
            volume_id=str(workspace.volume.id),
            kind=type(workspace).__name__,
            path=str(path),
        ) as span:
            async with self.lock(workspace.volume.id):
                if path.is_dir():
                    # Where every turn after a thread's first arrives. Saying so is
                    # what leaves the forks findable among them.
                    span.set_attribute("forked", False)
                    return path
                span.set_attribute("forked", True)
                path.parent.mkdir(parents=True, exist_ok=True)
                # Hidden, and named after the volume, so it is never mistaken for a
                # tree and the next fork of this volume reclaims whatever a killed
                # one left — the case the cleanup below cannot reach.
                staging = path.with_name(f".{workspace.volume.id}.forking")
                if staging.exists():
                    shutil.rmtree(staging)
                try:
                    await self.mirrors.export(
                        mirror, staging, tree_ref(workspace.volume.id)
                    )
                    await self.inherit_remotes(mirror, staging)
                    await self.checkout(workspace.volume, staging)
                    await install(staging)
                    staging.rename(path)
                except BaseException:
                    shutil.rmtree(staging, ignore_errors=True)
                    raise
            return path

    async def inherit_remotes(self, mirror: Path, workspace: Path) -> None:
        """Give the fork the mirror's own `origin`, or no remote where the mirror
        has none.

        The mirror's `origin` is the project's real upstream, and is what an agent
        means by pushing — a branch it named for the job, on the repository people
        will read it in. Nothing Octomate does travels that way: a turn is saved by
        pushing to the mirror's path, never through a remote name.

        A folder project's mirror has no upstream to inherit, and neither does its
        fork. There is nowhere to push a directory, and saying so by having no
        remote is clearer to an agent than an `origin` that fails when it is used.
        """
        if "origin" in (await run_git("remote", cwd=workspace)).split():
            await run_git("remote", "remove", "origin", cwd=workspace)
        if "origin" not in (await run_git("remote", cwd=mirror)).split():
            return
        url = await run_git("remote", "get-url", "origin", cwd=mirror)
        await run_git("remote", "add", "origin", url.strip(), cwd=workspace)

    async def checkout(self, volume: TreeVolume, workspace: Path) -> None:
        """Put a freshly forked `workspace` back the way this tree's last turn left
        it, when the export includes its saved snapshot.

        A thread whose workspace was pruned resumes here, and resumes into the
        state it was interrupted in rather than a tidied version of it: the branch
        goes back to where the agent's HEAD was, which is the snapshot's parent,
        and the tree it could see is laid down over that from the snapshot itself.
        The index then drops back to HEAD, so work the agent had not committed
        reads as uncommitted rather than as staged.

        A thread forking for the first time starts on its own branch, where the
        mirror's branch is — or where the volume's `base_ref` names, when the bind
        asked for one.
        The default branch is the wrong answer often enough: continuing someone's
        feature branch, reproducing against a tag, working from a PR head.

        The ref is only consulted by a first fork. A tree that was released resumes
        into its own snapshot, which is where its work is, and no starting point
        competes with that.
        """
        thread_id, ref = volume.thread_id, volume.base_ref
        saved = tree_ref(volume.id)
        # The fork's two endings, and the only place that knows which this is: a
        # thread starting, or one coming back to work a sweep reclaimed the tree
        # of. `resumed` is what separates them in a trace, and the snapshot it
        # came back from is what makes the tree it landed on checkable.
        with workspace_logfire.span(
            "workspace.checkout",
            thread_id=str(thread_id),
            volume_id=str(volume.id),
            ref=ref,
        ) as span:
            if not await run_git(
                "for-each-ref",
                "--format=%(refname)",
                saved,
                cwd=workspace,
                isolated=True,
            ):
                span.set_attribute("resumed", False)
                # Resolve only in the exported object store. A user-supplied ref
                # must never trigger a fetch of private mirror objects.
                start = (
                    await run_git(
                        "rev-parse",
                        "--verify",
                        "--end-of-options",
                        f"{ref if ref is not None else 'HEAD'}^{{commit}}",
                        cwd=workspace,
                        isolated=True,
                    )
                ).strip()
                await run_git(
                    "checkout",
                    "-b",
                    thread_branch(thread_id),
                    start,
                    cwd=workspace,
                    isolated=True,
                )
                return
            span.set_attribute("resumed", True)
            subject = await run_git(
                "show", "-s", "--format=%s", saved, cwd=workspace, isolated=True
            )
            branch = subject.strip().removeprefix(SNAPSHOT_PREFIX)
            span.set_attribute("branch", branch)
            span.set_attribute(
                "snapshot",
                (
                    await run_git(
                        "rev-parse",
                        saved,
                        cwd=workspace,
                        isolated=True,
                    )
                ).strip(),
            )
            head = (
                ("--detach", f"{saved}^")
                if branch == "HEAD"
                else ("-B", branch, f"{saved}^")
            )
            await run_git("checkout", *head, cwd=workspace, isolated=True)
            await run_git(
                "read-tree", "-u", "--reset", saved, cwd=workspace, isolated=True
            )
            await run_git("reset", cwd=workspace, isolated=True)

    async def save(self, thread: Thread, owner_id: UUID7) -> None:
        """Leave this owner's finished turn where losing their tree cannot cost it:
        a validated snapshot imported into the project's mirror as the tree's
        own ref.

        A fork owns its objects outright — nothing in a tree survives its
        removal — so without the import the tree is the only copy of
        what the turn did, and releasing it would be losing work rather than
        evicting a cache. That is what makes a tree disposable, and why the sweep
        is a disk decision.

        A thread with no tree for this owner — in no project, or one whose turn
        never forked one — has nothing to leave.

        The ref records what the tree holds rather than a history to protect.
        Imports update only that ref in the mirror and never reach the upstream.

        A failure is logged rather than raised. The turn has already happened and
        already answered; failing it now would report a delivered answer as an
        error, and the next turn saves the same work again.
        """
        project = await self.projects.of(thread)
        if project is None:
            return
        volume = await self.find(owner_id, thread.id, project.id)
        if volume is not None:
            await self.checkpoint(
                ProjectWorkspace(
                    self.path(volume),
                    thread.id,
                    volume,
                    project,
                    root=self.root(owner_id),
                )
            )

    async def checkpoint(self, workspace: ProjectWorkspace) -> None:
        """Checkpoint this exact volume without changing the user's index or tree.

        Validate the capture in a host-owned repository before importing it. A
        failed import leaves the tree unsaved and unreclaimable; an unchanged
        checkpoint needs no second transfer.
        """
        path = workspace.path
        with workspace_logfire.span(
            "workspace.save",
            thread_id=str(workspace.thread_id),
            volume_id=str(workspace.volume.id),
            project=workspace.project.name,
            saved=False,
        ) as span:
            if not path.is_dir():
                return
            try:
                async with (
                    self.lock(workspace.volume.id),
                    Snapshot.capture(path, self.identity) as snapshot,
                ):
                    await snapshot.save(
                        self.mirrors.path(workspace.project),
                        tree_ref(workspace.volume.id),
                    )
                    span.set_attribute("snapshot", snapshot.commit)
                span.set_attribute("saved", True)
            except (GitCommandError, OSError):
                logger.exception(
                    "the tree for thread %s could not be saved; its work is only in "
                    "%s until a later turn saves it",
                    workspace.thread_id,
                    path,
                )

    async def saved(self, volume: TreeVolume) -> bool:
        """Whether this volume matches its canonical snapshot in the host mirror.

        Workspace markers are writable by the harness and cannot authorize
        reclamation. Invalid or unreadable captures leave the tree unsaved.
        """
        project = next(
            (
                project
                for project in self.projects.list()
                if project.id == volume.project_id
            ),
            None,
        )
        if project is None:
            return False
        mirror = self.mirrors.path(project)
        if not mirror.is_dir():
            return False
        ref = tree_ref(volume.id)
        try:
            if not await run_git(
                "for-each-ref", "--format=%(objectname)", ref, cwd=mirror, isolated=True
            ):
                return False
            async with Snapshot.capture(self.path(volume), self.identity) as snapshot:
                return await snapshot.matches(mirror, ref)
        except (GitCommandError, OSError):
            logger.exception(
                "The tree %s could not be verified; keeping its work", volume.id
            )
            return False

    async def prune(self, idle: float) -> list[UUID7]:
        """Release every project tree nothing has used for `idle` seconds, and
        answer with the volumes that lost their folder.

        Eviction, not a decision about whether a thread is over: abandonment is
        unobservable, so rather than guessing, being wrong here costs the next turn
        a fork instead of costing anyone their work. Which is only true of a tree
        the mirror already has, so one holding anything unsaved is kept and said
        out loud. Idleness is read from the row's `last_used_at`, which every
        `open` stamps.

        A folder in a user's root that no row owns is reported and never deleted:
        the row is written before the folder, so such a folder is something this
        manager did not make.
        """
        released: list[UUID7] = []
        # Three counts, because "why is that tree still there" has three answers
        # and the disk cannot tell them apart.
        kept_busy = kept_unsaved = 0
        cutoff = datetime.now(UTC) - timedelta(seconds=idle)
        with workspace_logfire.span("workspace.prune", idle=idle) as span:
            async with async_session() as session:
                # Chat trees are their run's to throw away, and are never saved
                # anywhere a resume could restore them from.
                trees = await session.list(
                    TreeVolume,
                    limit=None,
                    expressions=[TreeVolume["project_id"].is_not(None)],
                )
            for volume in trees:
                path = self.path(volume)
                if not path.is_dir():
                    continue
                if volume.last_used_at > cutoff:
                    kept_busy += 1
                    continue
                if not await self.saved(volume):
                    kept_unsaved += 1
                    logger.warning(
                        "the tree %s for thread %s is idle but holds work the mirror "
                        "does not have; keeping it",
                        volume.id,
                        volume.thread_id,
                    )
                    continue
                await self.release(volume)
                released.append(volume.id)
            span.set_attribute("released", len(released))
            span.set_attribute("kept_busy", kept_busy)
            span.set_attribute("kept_unsaved", kept_unsaved)
            span.set_attribute("unowned", await self.report_unowned())
            if released:
                logger.info("released %d idle tree(s)", len(released))
            return released

    async def report_unowned(self) -> int:
        """Warn about every tree folder no row owns, and answer how many there
        are. Hidden folders are forks in progress, and are skipped."""
        named: dict[uuid.UUID, Path] = {}
        unowned: list[Path] = []
        for path in self.volumes_dir.glob("*/tree/[!.]*"):
            try:
                named[uuid.UUID(path.name)] = path
            except ValueError:
                unowned.append(path)
        if named:
            async with async_session() as session:
                owned = await session.list(
                    TreeVolume,
                    limit=None,
                    expressions=[TreeVolume["id"].in_(list(named))],
                )
            owned_ids = {volume.id for volume in owned}
            unowned += [path for key, path in named.items() if key not in owned_ids]
        for path in unowned:
            logger.warning("%s has no volume row; leaving it", path)
        return len(unowned)

    async def sweep(self) -> None:
        """Prune on the interval, for as long as the host runs.

        A sweep that fails is logged and the next one still happens: reclaiming
        disk is maintenance, and a host that stops serving because it could not
        tidy up has the priority backwards.
        """
        while True:
            await asyncio.sleep(self.config.sweep_interval)
            try:
                await self.prune(self.config.idle_window)
            except Exception:
                logger.exception("the workspace sweep failed")

    async def carry(self, source: Thread, thread: Thread, owner_id: UUID7) -> None:
        """Give `thread`, landed in `source`'s project, a tree holding this owner's
        work as `source` left it — what a conversation moving to another thread
        takes of its work — and fork it.

        Saved first: the source's last turn may not have been, and what it left
        uncommitted has to travel as uncommitted. The snapshot is filed under the
        new tree's ref too, so its first fork resumes into it the way a released
        tree does; on a branch of its own when the source was on its own, since
        two trees pushing one branch would overwrite each other. A source with no
        snapshot leaves `thread` to fork the project fresh.

        Ignored files are copied across, an `.env` among them, since a snapshot
        never holds them. Ignored directories are not: they are dependencies and
        build output, which the fork's install and the next build make again.
        """
        project = await self.projects.of(source)
        if project is None:
            return
        await self.save(source, owner_id)
        origin = await self.find(owner_id, source.id, project.id)
        landing = await self.resolve(thread.id, project, owner_id)
        mirror = self.mirrors.path(project)
        if origin is not None and await run_git(
            "ls-remote", str(mirror), tree_ref(origin.id), isolated=True
        ):
            saved = tree_ref(origin.id)
            subject = await run_git(
                "show", "-s", "--format=%s", saved, cwd=mirror, isolated=True
            )
            filed = saved
            if subject.strip() == f"{SNAPSHOT_PREFIX}{thread_branch(source.id)}":
                filed = await run_git(
                    *self.identity.commit_flags,
                    "commit-tree",
                    f"{saved}^{{tree}}",
                    "-p",
                    f"{saved}^",
                    "-m",
                    f"{SNAPSHOT_PREFIX}{thread_branch(thread.id)}",
                    cwd=mirror,
                    isolated=True,
                )
            await run_git(
                "update-ref",
                tree_ref(landing.volume.id),
                filed.strip(),
                cwd=mirror,
                isolated=True,
            )
        landed = await self.prepare(landing)
        if origin is None or not self.path(origin).is_dir():
            return
        await copy_ignored(self.path(origin), landed)

    async def dismiss(self, thread: Thread, owner_id: UUID7) -> bool:
        """Release this owner's tree on their agent's word that the work in it is
        done — the one release that is asked for rather than swept up — and answer
        whether it went.

        Saved first, and released only when the save took: the sweep's rule, that
        a tree holding work the mirror has not seen is never thrown away, does not
        bend for being asked. So what this costs is what a prune costs, a fork on
        the thread's next turn with the ref laid back over it.
        """
        with workspace_logfire.span(
            "workspace.dismiss", thread_id=str(thread.id), released=False
        ) as span:
            await self.save(thread, owner_id)
            project = await self.projects.of(thread)
            if project is None:
                return False
            volume = await self.find(owner_id, thread.id, project.id)
            if volume is None or not self.path(volume).is_dir():
                return False
            if not await self.saved(volume):
                logger.warning(
                    "the tree %s for thread %s was dismissed but holds work the "
                    "mirror does not have; keeping it",
                    volume.id,
                    thread.id,
                )
                return False
            await self.release(volume)
            span.set_attribute("released", True)
            return True

    async def release(self, volume: TreeVolume) -> None:
        """Give this tree's folder back to the disk, or nothing when it is already
        gone. The row stays, so the next fork lands at the same path.

        The mirror is untouched: each fork owns its objects outright. In a
        thread, off the loop: a released tree is as big as the project.
        """
        path = self.path(volume)
        # The end of a tree, and the span a resume is read against: a
        # `materialize` that forks the same volume is this one's other half.
        with workspace_logfire.span(
            "workspace.release",
            thread_id=str(volume.thread_id),
            volume_id=str(volume.id),
        ) as span:
            async with self.lock(volume.id):
                # False where the folder is already gone, or a sweep and a caller
                # both reached for the same tree.
                span.set_attribute("removed", path.is_dir())
                if path.is_dir():
                    await asyncio.to_thread(shutil.rmtree, path)
