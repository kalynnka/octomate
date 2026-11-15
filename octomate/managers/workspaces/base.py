"""Compose thread workspaces from logical volumes and their host storage."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import UUID7

from octomate.config.mirrors import GitIdentity
from octomate.config.workspaces import WorkspacesConfig
from octomate.managers.base import Manager
from octomate.managers.project import ProjectManager
from octomate.managers.storages import HostStorageManager
from octomate.managers.volumes import VolumeManager
from octomate.managers.workspaces.mirrors import MirrorManager
from octomate.managers.workspaces.transfers import copy_ignored
from octomate.schemas.project import Project
from octomate.schemas.thread import Thread
from octomate.schemas.volumes import GitVolume, UnregisteredProjectError, Volume
from octomate.schemas.volumes.git import (
    SNAPSHOT_PREFIX,
    GitCommandError,
    Snapshot,
    run_git,
)
from octomate.schemas.workspaces import Workspace, WorkspaceVolumes
from octomate.telemetry import workspace_logfire

logger = logging.getLogger(__name__)


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
        projects: ProjectManager,
        volumes: VolumeManager,
        mirrors: MirrorManager,
        config: WorkspacesConfig | None = None,
    ) -> None:
        self.projects: ProjectManager = projects
        self.volumes: VolumeManager = volumes
        self.mirrors: MirrorManager = mirrors
        self.config = config if config is not None else WorkspacesConfig()

    @property
    def host_storage(self) -> HostStorageManager:
        return self.volumes.host_storage

    @property
    def volumes_dir(self) -> Path:
        return self.host_storage.volumes_dir

    @property
    def identity(self) -> GitIdentity:
        """Who the machine is when it writes a snapshot — the `mirrors:` block's,
        read rather than copied, so a turn's work and a folder sync into a mirror
        carry one name and cannot drift into two."""
        return self.mirrors.config.identity

    def root(self, owner_id: UUID7) -> Path:
        """The owner's storage directory; volumes select individual host directories.

        Resolving it creates neither files nor database rows.
        """
        return self.volumes_dir / str(owner_id)

    async def resolve(
        self,
        thread_id: UUID7,
        project: Project | None,
        owner_id: UUID7 | None,
        ref: str | None = None,
    ) -> Workspace:
        """Record the owner's volumes and settle their paths without making files.

        A registered owner's tree comes with their package cache and their agent
        configuration. Unregistered
        senders receive a temporary chat path alone and cannot resolve project
        workspaces. This writes or stamps registered volume rows; use
        `volumes.find` for read-only inspection.
        """
        if owner_id is None and project is not None:
            raise UnregisteredProjectError(
                f"project {project.name!r} needs a registered user"
            )
        if owner_id is None:
            volumes: WorkspaceVolumes = {
                "work": Volume(
                    storage=self.host_storage.temporary(thread_id), ephemeral=True
                )
            }
        else:
            # The owner's own volumes need nothing from the mirror or the tree,
            # so they are settled while those are.
            async with asyncio.TaskGroup() as tasks:
                cache = tasks.create_task(self.volumes.slot(owner_id, "cache"))
                config = tasks.create_task(self.volumes.slot(owner_id, "config"))
                work = await self.volumes.tree(
                    owner_id,
                    thread_id,
                    project.id if project else None,
                    ref,
                    mirror=await self.mirrors.resolve(project),
                )
            volumes = {
                "work": work,
                "cache": cache.result(),
                "config": config.result(),
            }
        return Workspace(
            thread_id=thread_id,
            project=project,
            volumes=volumes,
            identity=self.identity,
        )

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
    async def enter(self, workspace: Workspace) -> AsyncGenerator[Workspace]:
        """Prepare the working tree's source, then enter the volume composition."""
        if isinstance(workspace.volume, GitVolume):
            if workspace.project is None:
                await self.mirrors.create()
            elif not workspace.path.is_dir():
                await self.mirrors.sync(workspace.project)
        async with workspace():
            yield workspace

    async def prepare(self, workspace: Workspace) -> Path:
        """Prepare a persistent project tree for a handoff outside an invocation."""
        volume = workspace.volume
        if workspace.project is None or not isinstance(volume, GitVolume):
            raise ValueError("A project handoff requires a Git working volume")
        if workspace.path.is_dir():
            return workspace.path
        mirror = await self.mirrors.sync(workspace.project)
        return await volume.materialize(mirror)

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
                await self.mirrors.resolve(project),
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
        mirror = (await self.mirrors.resolve(project)).path
        if not mirror.is_dir():
            return False
        ref = volume.snapshot_ref
        try:
            if not await run_git(
                "for-each-ref", "--format=%(objectname)", ref, cwd=mirror, isolated=True
            ):
                return False
            async with Snapshot.capture(volume.path, self.identity) as snapshot:
                return await snapshot.matches(mirror, ref)
        except (GitCommandError, OSError):
            logger.exception(
                "The tree %s could not be verified; keeping its work", volume.id
            )
            return False

    async def reclaim(self, volume: GitVolume) -> bool:
        """Delete a tree's folder only while nothing uses it and the mirror has
        its work, and answer whether it went.

        The storage lock is held from the first check to the deletion. A turn
        that arrives meanwhile waits and forks afresh, instead of starting in a
        folder that is about to go or leaving work there after it was verified.
        """
        storage = volume.storage
        async with storage.lock:
            if storage.uses:
                return False
            if not await self.saved(volume):
                logger.warning(
                    "the tree %s for thread %s holds work the mirror does not "
                    "have; keeping it",
                    volume.id,
                    volume.thread_id,
                )
                return False
            await storage.release()
        return True

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
                path = volume.path
                if not path.is_dir():
                    continue
                if volume.last_used_at > cutoff:
                    kept_busy += 1
                elif await self.reclaim(volume):
                    released.append(volume.id)
                # Read before anything else can run: `reclaim` refused under the
                # lock a moment ago, so the use count still says which refusal.
                elif volume.storage.uses:
                    kept_busy += 1
                else:
                    kept_unsaved += 1
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
        build output, which whatever runs in the landed tree makes again.
        """
        project = await self.projects.of(source)
        if project is None:
            return
        await self.save(source, owner_id)
        origin = await self.volumes.find(owner_id, source.id, project.id)
        landing = await self.resolve(thread.id, project, owner_id)
        volume = landing.volume
        if not isinstance(volume, GitVolume):
            raise ValueError("A project handoff requires a Git working volume")
        mirror = (await self.mirrors.resolve(project)).path
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
                    f"{SNAPSHOT_PREFIX}{volume.branch}",
                    cwd=mirror,
                    isolated=True,
                )
            await run_git(
                "update-ref",
                volume.snapshot_ref,
                filed.strip(),
                cwd=mirror,
                isolated=True,
            )
        landed = await self.prepare(landing)
        if origin is None or not origin.path.is_dir():
            return
        await copy_ignored(origin.path, landed)

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
            if volume is None or not volume.path.is_dir():
                return False
            released = await self.reclaim(volume)
            span.set_attribute("released", released)
            return released
