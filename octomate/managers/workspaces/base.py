"""Thread workspaces: the trees a run happens in, kept as volumes in each user's
root, and their lifecycle from open to save to release."""

from __future__ import annotations

import asyncio
import logging
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
from octomate.managers.base import Manager
from octomate.managers.project import ProjectManager
from octomate.managers.volumes import VolumeManager
from octomate.managers.workspaces.mirrors import (
    GitCommandError,
    MirrorManager,
    run_git,
)
from octomate.managers.workspaces.snapshots import SNAPSHOT_PREFIX, Snapshot
from octomate.managers.workspaces.transfers import copy_ignored
from octomate.schemas.project import Project
from octomate.schemas.thread import Thread
from octomate.schemas.volumes import GitVolume, Volume
from octomate.telemetry import workspace_logfire
from octomate.volumes import HostDirectory

logger = logging.getLogger(__name__)


class UnregisteredProjectError(PermissionError):
    """An unregistered sender asked to work in a project. A project's tree lives in
    a registered user's root, so an unregistered sender may only chat."""


@dataclass
class Workspace(ABC):
    """A run's directory and identity; volumes own its content lifecycle."""

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

    volume: GitVolume
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


class WorkspaceManager(Manager):
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
        volumes: VolumeManager | None = None,
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
        self.volumes: VolumeManager = (
            volumes if volumes is not None else VolumeManager()
        )
        self.mirrors = mirrors if mirrors is not None else MirrorManager()
        self.config = config if config is not None else WorkspacesConfig()
        # Resolved once, here: a volume path leaves this manager to become a
        # subprocess's cwd and a `GIT_INDEX_FILE` beside it, and the default is
        # relative to wherever Octomate was started.
        self.volumes_dir = volumes_dir.resolve()
        self.backings: dict[Path, HostDirectory] = {}

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

    def path(self, volume: Volume) -> Path:
        """Where this volume's folder lives in its owner's root. Derived from the
        row's identity, never stored, so the store can change without a
        migration."""
        return (
            self.root(volume.owner_id) / (volume.usage or volume.kind) / str(volume.id)
        )

    @overload
    async def resolve(
        self,
        thread_id: UUID7,
        project: Project,
        owner_id: UUID7 | None,
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
        `volumes.find` and `path` for read-only inspection.
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
            volume = await self.volumes.tree(owner_id, thread_id, None)
            return ChatWorkspace(
                self.path(volume),
                thread_id,
                volume,
                root=self.root(owner_id),
            )
        volume = await self.volumes.tree(owner_id, thread_id, project.id, ref)
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
        owner_id: UUID7 | None,
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

    def backing(self, path: Path) -> HostDirectory:
        """Share a directory's lock and active uses across resolved workspace views."""
        path = path.absolute()
        if path not in self.backings:
            self.backings[path] = HostDirectory(path)
        return self.backings[path]

    @asynccontextmanager
    async def enter[WorkspaceT: Workspace](
        self, workspace: WorkspaceT
    ) -> AsyncGenerator[WorkspaceT]:
        """Adapt the current workspace view to the volume-owned lifecycle."""
        backing = self.backing(workspace.path)
        if isinstance(workspace, ProjectWorkspace | ChatWorkspace):
            if isinstance(workspace, ProjectWorkspace):
                mirror = self.mirrors.path(workspace.project)
                if not workspace.path.is_dir():
                    mirror = await self.mirrors.sync(workspace.project)
            else:
                mirror = await self.mirrors.create()
            async with workspace.volume(backing, mirror=mirror, identity=self.identity):
                yield workspace
            return
        if not isinstance(workspace, UnregisteredWorkspace):
            raise TypeError(f"Unsupported workspace: {type(workspace).__name__}")
        async with backing.use(ephemeral=True) as path:
            await anyio.Path(path).mkdir(parents=True, exist_ok=True)
            yield workspace

    async def prepare(self, workspace: ProjectWorkspace) -> Path:
        """Prepare a persistent project tree for a handoff outside an invocation."""
        if workspace.path.is_dir():
            return workspace.path
        mirror = await self.mirrors.sync(workspace.project)
        return await workspace.volume.materialize(self.backing(workspace.path), mirror)

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
        volume = await self.volumes.find(owner_id, thread.id, project.id)
        if volume is not None:
            await volume.checkpoint(
                self.backing(self.path(volume)),
                self.mirrors.path(project),
                self.identity,
            )

    async def saved(self, volume: GitVolume) -> bool:
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
        ref = volume.snapshot_ref
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
            trees = await self.volumes.project_trees()
            for volume in trees:
                path = self.path(volume)
                if not path.is_dir():
                    continue
                if self.backing(path).uses or volume.last_used_at > cutoff:
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
                await self.backing(self.path(volume)).release()
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
            owned_ids = await self.volumes.owned(list(named))
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
        origin = await self.volumes.find(owner_id, source.id, project.id)
        landing = await self.resolve(thread.id, project, owner_id)
        mirror = self.mirrors.path(project)
        if origin is not None and await run_git(
            "ls-remote", str(mirror), origin.snapshot_ref, isolated=True
        ):
            saved = origin.snapshot_ref
            subject = await run_git(
                "show", "-s", "--format=%s", saved, cwd=mirror, isolated=True
            )
            filed = saved
            if subject.strip() == f"{SNAPSHOT_PREFIX}{origin.branch}":
                filed = await run_git(
                    *self.identity.commit_flags,
                    "commit-tree",
                    f"{saved}^{{tree}}",
                    "-p",
                    f"{saved}^",
                    "-m",
                    f"{SNAPSHOT_PREFIX}{landing.volume.branch}",
                    cwd=mirror,
                    isolated=True,
                )
            await run_git(
                "update-ref",
                landing.volume.snapshot_ref,
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
            volume = await self.volumes.find(owner_id, thread.id, project.id)
            if (
                volume is None
                or not self.path(volume).is_dir()
                or self.backing(self.path(volume)).uses
            ):
                return False
            if not await self.saved(volume):
                logger.warning(
                    "the tree %s for thread %s was dismissed but holds work the "
                    "mirror does not have; keeping it",
                    volume.id,
                    thread.id,
                )
                return False
            await self.backing(self.path(volume)).release()
            span.set_attribute("released", True)
            return True
