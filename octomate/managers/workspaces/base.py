"""Thread workspaces: the trees a run happens in, kept as volumes in each user's
root, and their lifecycle from open to save to release."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Self, overload

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
from octomate.schemas.project import Project
from octomate.schemas.thread import Thread
from octomate.schemas.volumes import CacheVolume, MemoryVolume, TreeVolume
from octomate.telemetry import workspace_logfire

logger = logging.getLogger(__name__)

# `cp` forks a file by cloning its extents instead of copying its bytes, with `-c`
# on BSD and `--reflink=always` on GNU. Each flag is an error on the other's `cp`,
# and neither says anything about the filesystem underneath, so which one works is
# probed rather than dispatched on by platform.
REFLINK_FLAGS = ("-c", "--reflink=always")


# The snapshot the last push carried, kept in the workspace itself. A workspace
# that no longer snapshots to the same thing holds work the mirror has never
# seen, which is the one thing the pruner may not throw away.
SAVED_REF = "refs/octomate/saved"

# What a snapshot's message carries, so a resume knows which branch to put the
# workspace back on. A detached HEAD is called `HEAD`, git's own name for it,
# which is also how it goes back.
SNAPSHOT_PREFIX = "octomate: "


def tree_ref(volume_id: UUID7) -> str:
    """Where a tree's saved work is kept in its project's mirror.

    Outside `refs/heads/`, so the mirror does not grow a branch per tree. Keyed by
    the tree's volume rather than its thread, so two users working in one shared
    thread never save over each other. That a fork holds none of these refs is
    `discard_octomate_refs`' doing — a copied fork brings across whatever the
    mirror has.
    """
    return f"refs/octomate/trees/{volume_id}"


def thread_branch(thread_id: uuid.UUID) -> str:
    """The branch a thread's first fork starts on, its own."""
    return f"octomate/thread-{thread_id}"


class UnregisteredProjectError(PermissionError):
    """An unregistered sender asked to work in a project. A project's tree lives in
    a registered user's root, so an unregistered sender may only chat."""


class CopyError(RuntimeError):
    """A `cp` exited nonzero; the message carries its stderr."""


async def copy(source: Path, target: Path, flag: str) -> None:
    """Copy `source` to `target` with `flag`, raising `CopyError` with the stderr
    when `cp` exits nonzero.

    `-a` because a fork has to be the tree it was made from: a symlink stays a
    symlink, and preserved timestamps keep the copied git index valid, so the
    workspace's first `git status` compares stat data rather than rehashing
    every file in the tree.
    """
    process = await asyncio.create_subprocess_exec(
        "cp",
        "-a",
        flag,
        str(source),
        str(target),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise CopyError(
            f"cp -a {flag} {source} {target} failed: "
            f"{stderr.decode(errors='replace').strip()}"
        )


@dataclass
class Workspace(ABC):
    """The directory one run happens in, and what becomes of it when the run ends.

    Handed out by `WorkspaceManager.open` and entered by the run that was given it.
    Its path is settled when it is opened, before anything is made on disk, which
    is what lets a runtime be configured with its own working directory before the
    process that will run there exists.
    """

    workspaces: WorkspaceManager
    thread_id: UUID7

    @property
    @abstractmethod
    def path(self) -> Path:
        """Where this run happens. Absolute, because it becomes the working
        directory of a process Octomate did not start in its own, and known
        without touching the disk."""

    @abstractmethod
    async def prepare(self) -> Path:
        """The directory, made and ready for a run to happen in."""

    async def __aenter__(self) -> Self:
        await self.prepare()
        return self

    @abstractmethod
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """End the run's claim on the directory, however the run ended."""


@dataclass
class TreeWorkspace(Workspace, ABC):
    """A tree volume in a registered user's root: a fork of a mirror, checked out
    on the thread's own branch.

    The volume's row outlives its folder. Releasing a tree deletes the folder and
    keeps the row, so the next fork lands at the same path — which is what lets a
    harness resume a session it keyed by its working directory.
    """

    volume: TreeVolume

    @property
    def path(self) -> Path:
        return self.workspaces.path(self.volume)


@dataclass
class ProjectWorkspace(TreeWorkspace):
    """This user's own checkout of the thread's project.

    A thread runs here rather than in `project.root`, so two threads on one
    project, or two users in one thread, cannot walk over each other's uncommitted
    work — and the root stays the person's, untouched by anything an agent does.
    """

    project: Project

    async def prepare(self) -> Path:
        """This checkout, resumed as it stands or forked when it is gone.

        Only a tree's first turn, or the first after a release, does any of that. A
        tree that exists is resumed into as it stands, and its mirror is not synced
        for it either: a fetch would buy the turn nothing and would couple
        continuing a thread to the upstream being reachable.
        """
        if self.path.is_dir():
            return self.path
        mirror = await self.workspaces.mirrors.sync(self.project)
        return await self.workspaces.materialize(self, mirror)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Save completed or failed work; cancellation leaves the tree in place."""
        if exc_type is None or issubclass(exc_type, Exception):
            await self.save()

    async def save(self) -> None:
        """Checkpoint this exact volume without changing the user's index or tree.

        A failed push leaves the tree unsaved and unreclaimable. An unchanged
        checkpoint needs no second push when another lifecycle boundary closes.
        """
        manager = self.workspaces
        path = self.path
        with workspace_logfire.span(
            "workspace.save",
            thread_id=str(self.thread_id),
            volume_id=str(self.volume.id),
            project=self.project.name,
            saved=False,
        ) as span:
            if not path.is_dir():
                return
            try:
                async with manager.lock(self.volume.id):
                    if await manager.saved(path):
                        span.set_attribute("saved", True)
                        return
                    snapshot = await manager.snapshot(path)
                    await run_git(
                        "push",
                        "--force",
                        str(manager.mirrors.path(self.project)),
                        f"{snapshot}:{tree_ref(self.volume.id)}",
                        cwd=path,
                    )
                    await run_git("update-ref", SAVED_REF, snapshot, cwd=path)
                span.set_attribute("snapshot", snapshot)
                span.set_attribute("saved", True)
            except (GitCommandError, OSError):
                logger.exception(
                    "the tree for thread %s could not be saved; its work is only in "
                    "%s until a later turn saves it",
                    self.thread_id,
                    path,
                )


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

    async def prepare(self) -> Path:
        """An empty tree of this run's own, forked from the blank mirror.

        Claimed first, which throws away whatever a killed run left at this path:
        nothing resumes a chat tree, so anything there when nobody is in it belongs
        to a run that is over. Nothing here is ever saved.
        """
        await self.workspaces.claim(self)
        try:
            mirror = await self.workspaces.mirrors.create()
            return await self.workspaces.materialize(self, mirror)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await self.workspaces.discard(self)
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Throw the folder away and keep the row. In `__aexit__` rather than after
        the run's last event, so a turn that raised does not leave its files
        behind."""
        with anyio.CancelScope(shield=True):
            await self.workspaces.discard(self)


@dataclass
class UnregisteredWorkspace(Workspace):
    """Where an unregistered sender's run happens: an empty temporary directory,
    with no git and no volume, gone when the run ends.

    An unregistered sender has no root to hold a volume, so they may only chat.
    The path is the thread's, so a runtime that keys its session by working
    directory can still resume the conversation.
    """

    @property
    def key(self) -> UUID7:
        """What overlapping runs in this directory are counted by."""
        return self.thread_id

    @property
    def path(self) -> Path:
        return (
            Path(tempfile.gettempdir()) / "octomate-unregistered" / str(self.thread_id)
        ).resolve()

    async def prepare(self) -> Path:
        await self.workspaces.claim(self)
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            return self.path
        except BaseException:
            with anyio.CancelScope(shield=True):
                await self.workspaces.discard(self)
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        with anyio.CancelScope(shield=True):
            await self.workspaces.discard(self)


class WorkspaceManager(Manager, Locks[UUID7]):
    """Every run's workspace. A registered user's run gets a tree volume in that
    user's root, ``volumes_dir/<owner>/tree/<volume>``, recorded by a row and
    checked out on the thread's own branch; an unregistered sender's run gets an
    empty temporary directory.

    A fork is a complete, independent repository — its own `.git`, its own object
    store — made by cloning the mirror's extents where the filesystem does
    copy-on-write and by `git clone` where it does not. Never a `git worktree`,
    whose `.git` is a file pointing back at the mirror: commits in one write
    outside the workspace, which an agent's write sandbox refuses.

    A project's tree is a cache rather than the only copy: each turn's tree is
    snapshotted and pushed to the mirror under the tree's own ref, so releasing
    one is a disk decision and resuming lays that ref back over a fresh fork.
    Releasing deletes the folder and keeps the row. What the agent did with git in
    there is the agent's — nothing here commits.

    A registered user's thread in no project gets a tree too, forked from the blank
    mirror and thrown away when the run that made it ends. Same mechanism, opposite
    ending: there is no project to keep it for.

    A turn asks `open` for its workspace and enters it to prepare the directory.
    Project exit checkpoints its exact volume; chat exit releases its temporary
    directory. Explicit `save` and `dismiss` resolve an owner's existing tree for
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
        self.reflink: str | None = None
        self.probed = False
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

    def path(self, volume: TreeVolume | MemoryVolume | CacheVolume) -> Path:
        """Where this volume's folder lives in its owner's root. Derived from the
        row's identity, never stored, so the store can change without a
        migration."""
        return self.volumes_dir / str(volume.owner_id) / volume.kind / str(volume.id)

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
    async def open(
        self,
        thread_id: UUID7,
        project: Project,
        owner_id: UUID7,
        ref: str | None = None,
    ) -> ProjectWorkspace: ...

    @overload
    async def open(
        self,
        thread_id: UUID7,
        project: None,
        owner_id: UUID7,
        ref: None = None,
    ) -> ChatWorkspace: ...

    @overload
    async def open(
        self,
        thread_id: UUID7,
        project: Project | None,
        owner_id: UUID7 | None,
        ref: str | None = None,
    ) -> Workspace: ...

    async def open(
        self,
        thread_id: UUID7,
        project: Project | None,
        owner_id: UUID7 | None,
        ref: str | None = None,
    ) -> Workspace:
        """The workspace a run in this thread happens in, ready to be entered.

        A registered owner gets a tree in their own root: a checkout of the project
        that outlives the turn, or an empty tree that does not. An unregistered
        sender may only chat, in a temporary directory, and asking for a project
        raises `UnregisteredProjectError` so the caller can say so.

        `project` is passed rather than looked up because the caller has already
        asked. Only the row is written here; the folder is made when the run
        enters the workspace, so a runtime can be told where it will run first.
        """
        if owner_id is None:
            if project is not None:
                raise UnregisteredProjectError(
                    f"project {project.name!r} needs a registered user"
                )
            return UnregisteredWorkspace(self, thread_id)
        if project is None:
            return ChatWorkspace(
                self, thread_id, await self.tree(owner_id, thread_id, None)
            )
        volume = await self.tree(owner_id, thread_id, project.id, ref)
        return ProjectWorkspace(self, thread_id, volume, project)

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

    async def detect(self) -> str | None:
        """The `cp` flag that forks without copying bytes on this host, or None
        where nothing does and workspaces are cloned instead.

        Probed once, by actually cloning a file where workspaces land: the answer
        belongs to that filesystem rather than to the platform, and it cannot
        change under a running host. A host with no copy-on-write filesystem is
        not a failure — it pays a full tree per workspace and works.
        """
        if self.probed:
            return self.reflink
        self.volumes_dir.mkdir(parents=True, exist_ok=True)
        probe = Path(tempfile.mkdtemp(dir=self.volumes_dir))
        try:
            source = probe / "source"
            source.write_bytes(b"probe")
            for flag in REFLINK_FLAGS:
                try:
                    await copy(source, probe / "target", flag)
                except CopyError:
                    continue
                self.reflink = flag
                break
        finally:
            shutil.rmtree(probe)
        self.probed = True
        logger.info(
            "thread workspaces fork by %s",
            f"cp -a {self.reflink}" if self.reflink else "git clone",
        )
        return self.reflink

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

        It is installed before it is moved into place, so a workspace is ready to
        run the moment it exists. A copy inherits the mirror's installed trees and
        that costs nothing; a clone carries no untracked file, so an ext4 host
        builds its own out of the store the mirror already warmed.
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
                flag = await self.detect()
                # The one thing about a fork that another host would do differently,
                # so a trace of one says it before anything else.
                span.set_attribute("mechanism", f"cp -a {flag}" if flag else "clone")
                # Hidden, and named after the volume, so it is never mistaken for a
                # tree and the next fork of this volume reclaims whatever a killed
                # one left — the case the cleanup below cannot reach.
                staging = path.with_name(f".{workspace.volume.id}.forking")
                if staging.exists():
                    shutil.rmtree(staging)
                try:
                    if flag is not None:
                        await copy(mirror, staging, flag)
                    else:
                        await run_git("clone", str(mirror), str(staging))
                    await self.inherit_remotes(mirror, staging)
                    await self.discard_octomate_refs(staging)
                    await self.checkout(workspace.volume, mirror, staging)
                    await install(staging)
                    staging.rename(path)
                except BaseException:
                    shutil.rmtree(staging, ignore_errors=True)
                    raise
            return path

    async def inherit_remotes(self, mirror: Path, workspace: Path) -> None:
        """Give the fork the mirror's own `origin`, or no remote where the mirror
        has none.

        Otherwise where a workspace points depends on how it was forked: a copy
        inherits the mirror's remotes, and a clone gets an `origin` pointing at the
        mirror itself. An agent asked to push its branch would find a different
        answer on a copy-on-write host than on one that clones, which is not
        something the host's filesystem gets to decide.

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

    async def discard_octomate_refs(self, workspace: Path) -> None:
        """Take Octomate's own refs out of a fresh fork, however it was made.

        `git clone` asks for `refs/heads/*` and tags, so a cloned fork never had
        them. `cp` does not know what git is: it brings `.git` across whole, and
        with it every other thread's `refs/octomate/threads/*` — their snapshots
        sitting in a workspace that was never told about them, out of `git branch`
        but not out of `git log --all`. Which of the two a host does is `detect`'s
        answer about its filesystem, and what one thread can read of another is not
        something the filesystem gets to decide.

        The whole namespace rather than the foreign entries: a mirror that somehow
        carried a `refs/octomate/saved` would hand a fresh fork a workspace that
        reads as already saved, which the sweep answers by reclaiming work that was
        never pushed. Dropping this thread's own along with the rest costs nothing —
        the mirror is where it lives, and `checkout` fetches it back next.
        """
        listing = await run_git(
            "for-each-ref", "--format=%(refname)", "refs/octomate/", cwd=workspace
        )
        for ref in listing.split():
            await run_git("update-ref", "-d", ref, cwd=workspace)

    async def checkout(self, volume: TreeVolume, mirror: Path, workspace: Path) -> None:
        """Put a freshly forked `workspace` back the way this tree's last turn left
        it, when `mirror` is keeping a snapshot of it.

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
            if not await run_git("ls-remote", str(mirror), saved):
                span.set_attribute("resumed", False)
                branch = thread_branch(thread_id)
                if ref is None:
                    await run_git("checkout", "-b", branch, cwd=workspace)
                    return
                # Fetched from the mirror rather than named directly: a clone's
                # remote-tracking refs are removed with the `origin` `inherit_remotes`
                # replaces, so the only commit a fork is certain to resolve is the one
                # it was left on. A ref the mirror does not have fails here, which is
                # what the caller has to be able to report.
                await run_git("fetch", str(mirror), ref, cwd=workspace)
                await run_git("checkout", "-b", branch, "FETCH_HEAD", cwd=workspace)
                return
            span.set_attribute("resumed", True)
            # Every fork arrives without it, whichever way it was made, so this is how
            # the workspace comes to have it at all.
            await run_git("fetch", str(mirror), f"+{saved}:{saved}", cwd=workspace)
            subject = await run_git("show", "-s", "--format=%s", saved, cwd=workspace)
            branch = subject.strip().removeprefix(SNAPSHOT_PREFIX)
            span.set_attribute("branch", branch)
            span.set_attribute(
                "snapshot",
                (await run_git("rev-parse", saved, cwd=workspace)).strip(),
            )
            head = (
                ("--detach", f"{saved}^")
                if branch == "HEAD"
                else ("-B", branch, f"{saved}^")
            )
            await run_git("checkout", *head, cwd=workspace)
            await run_git("read-tree", "-u", "--reset", saved, cwd=workspace)
            await run_git("reset", cwd=workspace)
            # A restored workspace is the mirror's snapshot by construction, and says
            # so: the ref that records it was local to the workspace this one replaces,
            # and without this a resume that never reached its next save would look
            # like unsaved work forever, which is a workspace that cannot be reclaimed.
            await run_git("update-ref", SAVED_REF, saved, cwd=workspace)

    async def snapshot(self, path: Path) -> str:
        """A commit holding this workspace exactly as it stands — staged, unstaged
        and untracked alike — made without disturbing it.

        Built rather than committed, so restoring a workspace is all this costs
        the repository inside it: HEAD stays where the agent left it, its branch
        carries the agent's commits and no others, and whatever the agent was in
        the middle of is not finished on its behalf. Resolving that history is the
        agent's job; putting the tree back is this one.

        The parent is HEAD, so the branch's own history comes along, and the
        snapshot belongs to no branch — reachable only from the thread's ref, the
        way a stash is reachable only from `refs/stash`. Its message carries the
        branch, which is the one thing a tree and a parent cannot say.

        The index it is written through is a copy of the workspace's, which keeps
        the real one untouched and carries git's stat cache with it, so staging
        the tree compares timestamps instead of rehashing every file.
        """
        index = path / ".git" / "octomate-snapshot-index"
        await asyncio.to_thread(shutil.copyfile, path / ".git" / "index", index)
        env = {"GIT_INDEX_FILE": str(index)}
        await run_git("add", "-A", cwd=path, env=env)
        tree = (await run_git("write-tree", cwd=path, env=env)).strip()
        branch = (await run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=path)).strip()
        commit = await run_git(
            *self.identity.commit_flags,
            "commit-tree",
            tree,
            "-p",
            "HEAD",
            "-m",
            f"{SNAPSHOT_PREFIX}{branch}",
            cwd=path,
        )
        return commit.strip()

    async def save(self, thread: Thread, owner_id: UUID7) -> None:
        """Leave this owner's finished turn where losing their tree cannot cost it:
        a snapshot of the whole tree, pushed to the project's mirror as the tree's
        own ref.

        Snapshot *and* push. A fork owns its objects outright — nothing in a tree
        survives its removal — so without the push the tree is the only copy of
        what the turn did, and releasing it would be losing work rather than
        evicting a cache. That is what makes a tree disposable, and why the sweep
        is a disk decision.

        A thread with no tree for this owner — in no project, or one whose turn
        never forked one — has nothing to leave.

        Forced, because the ref records what the tree holds rather than a history
        to protect. The mirror is the only place this ever pushes; nothing here
        reaches the upstream.

        A failure is logged rather than raised. The turn has already happened and
        already answered; failing it now would report a delivered answer as an
        error, and the next turn saves the same work again.
        """
        project = await self.projects.of(thread)
        if project is None:
            return
        volume = await self.find(owner_id, thread.id, project.id)
        if volume is not None:
            await ProjectWorkspace(self, thread.id, volume, project).save()

    async def saved(self, path: Path) -> bool:
        """Whether the mirror already holds everything in this workspace: the same
        tree, commit and branch as the last push carried.

        The question the sweep has to answer before reclaiming anything, and one
        `git status` cannot answer any more — nothing here commits, so a saved
        workspace is a dirty workspace and being dirty says nothing. Snapshotting
        again and comparing does say it. A push that failed leaves a workspace
        that looks saved from the outside, which is why what it is compared
        against is a ref the push itself writes.
        """
        pushed = await run_git(
            "for-each-ref", "--format=%(objectname)", SAVED_REF, cwd=path
        )
        if not pushed.strip():
            return False
        state = ("show", "-s", "--format=%T %P %s")
        return await run_git(*state, pushed.strip(), cwd=path) == await run_git(
            *state, await self.snapshot(path), cwd=path
        )

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
                if not await self.saved(path):
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
        landing = await self.open(thread.id, project, owner_id)
        mirror = self.mirrors.path(project)
        if origin is not None and await run_git(
            "ls-remote", str(mirror), tree_ref(origin.id)
        ):
            saved = tree_ref(origin.id)
            subject = await run_git("show", "-s", "--format=%s", saved, cwd=mirror)
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
                )
            await run_git(
                "update-ref", tree_ref(landing.volume.id), filed.strip(), cwd=mirror
            )
        landed = await landing.prepare()
        if origin is None or not self.path(origin).is_dir():
            return
        source_path = self.path(origin)
        ignored = await run_git(
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "-z",
            cwd=source_path,
        )
        for name in ignored.split("\0"):
            if not name or name.endswith("/") or (landed / name).exists():
                continue
            (landed / name).parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                shutil.copy2, source_path / name, landed / name, follow_symlinks=False
            )

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
            if not await self.saved(self.path(volume)):
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

        The mirror is untouched: a copied fork owns its objects outright, and a
        cloned one holds hard links, where unlinking a link leaves the file. In a
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
