"""Logical volumes: persisted identity and policy, independent of their backing."""

from __future__ import annotations

import logging
import shutil
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Self

import anyio
from arcanus import BaseTransmuter
from arcanus.base import Identity
from pydantic import UUID7, AwareDatetime, ConfigDict, Field, model_validator
from uuid_utils.compat import uuid7

from octomate.config.mirrors import GitIdentity
from octomate.models import volumes as volume_models
from octomate.schemas.base import sqlalchemy_materia
from octomate.telemetry import volume_logfire
from octomate.types.volumes import VolumeKind, VolumeUsage
from octomate.volumes import HostDirectory

logger = logging.getLogger(__name__)


@sqlalchemy_materia.bless(volume_models.Volume)
class Volume(BaseTransmuter):
    """A logical volume whose identity outlives any local directory or attachment.

    Operations change this transmuter directly. The caller's session owns
    persistence; paths, mounts and active uses belong to the runtime.
    """

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    owner_id: UUID7 = Field(
        frozen=True, description="The registered user who owns this logical volume."
    )
    kind: VolumeKind = Field(
        default="directory",
        frozen=True,
        description="Content behavior: a directory or a Git-tracked directory, independent of backing storage.",
    )
    usage: VolumeUsage | None = Field(
        default=None,
        frozen=True,
        description="Optional application use: working tree, user memory or package cache.",
    )
    ephemeral: bool = Field(
        default=False,
        frozen=True,
        description="Whether contents are discarded after the last active use; the logical identity remains.",
    )
    thread_id: UUID7 | None = Field(
        default=None,
        frozen=True,
        description="The thread using this working volume; absent for other uses.",
    )
    project_id: UUID7 | None = Field(
        default=None,
        frozen=True,
        description="The working volume's project; absent for chats and other uses.",
    )
    created_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(UTC),
        frozen=True,
        description="When the logical volume was registered.",
    )
    last_used_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the application last acquired this volume; used for idle reclamation.",
    )

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.usage == "tree":
            if self.thread_id is None:
                raise ValueError("A working-tree volume requires a thread")
        elif self.thread_id is not None or self.project_id is not None:
            raise ValueError(
                "Only working-tree volumes have thread or project associations"
            )
        return self

    @asynccontextmanager
    async def __call__(self, backing: HostDirectory) -> AsyncGenerator[Path]:
        """Prepare the directory and retain or release its contents on exit."""
        async with backing.use(ephemeral=self.ephemeral) as path:
            await anyio.Path(path).mkdir(parents=True, exist_ok=True)
            yield path

    def touch(self) -> None:
        """Record application use; the caller commits it with the owning session."""
        self.last_used_at = datetime.now(UTC)


@sqlalchemy_materia.bless(volume_models.GitVolume)
class GitVolume(Volume):
    """Git content and reference policy, usable without a thread or project."""

    kind: Literal["git"] = Field(default="git", frozen=True)
    base_ref: str | None = Field(
        default=None,
        frozen=True,
        description="The Git ref used for the first checkout; None selects the source's default branch.",
    )

    @property
    def snapshot_ref(self) -> str:
        """Stable private snapshot identity, preserved when backing is reclaimed."""
        return f"refs/octomate/trees/{self.id}"

    @property
    def branch(self) -> str:
        """Initial branch, preserving the existing spelling for thread volumes."""
        if self.thread_id is not None:
            return f"octomate/thread-{self.thread_id}"
        return f"octomate/volume-{self.id}"

    @asynccontextmanager
    async def __call__(
        self,
        backing: HostDirectory,
        *,
        mirror: Path | None = None,
        identity: GitIdentity | None = None,
    ) -> AsyncGenerator[Path]:
        """Prepare Git content and checkpoint persistent work after a completed run.

        Cancellation leaves persistent files in place without capturing a partial
        turn. Temporary contents are released after their last active use.
        """
        if mirror is None:
            raise ValueError("A Git volume requires a source mirror")
        identity = identity if identity is not None else GitIdentity()
        async with backing.use(ephemeral=self.ephemeral) as path:
            await self.materialize(backing, mirror)
            save = True
            try:
                yield path
            except BaseException as error:
                save = isinstance(error, Exception)
                raise
            finally:
                if save and not self.ephemeral:
                    await self.checkpoint(backing, mirror, identity)

    async def materialize(self, backing: HostDirectory, mirror: Path) -> Path:
        """Export only authorized history and this volume's snapshot atomically."""
        # The legacy workspace package imports this schema during initialization.
        from octomate.managers.workspaces.dependencies import install
        from octomate.managers.workspaces.mirrors import MirrorManager

        path = backing.path
        # Outside the lock, so a turn waiting on another turn's fork is time this
        # span accounts for. From the far end the wait and a slow fork look the
        # same, and they want opposite fixes.
        with volume_logfire.span(
            "volume.materialize",
            thread_id=str(self.thread_id),
            volume_id=str(self.id),
            kind=self.kind,
            path=str(path),
        ) as span:
            async with backing.lock:
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
                staging = path.with_name(f".{self.id}.forking")
                if staging.exists():
                    shutil.rmtree(staging)
                try:
                    await MirrorManager.export(mirror, staging, self.snapshot_ref)
                    await self.inherit_remotes(mirror, staging)
                    await self.checkout(staging)
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
        # The legacy workspace package imports this schema during initialization.
        from octomate.managers.workspaces.mirrors import run_git

        if "origin" in (await run_git("remote", cwd=workspace)).split():
            await run_git("remote", "remove", "origin", cwd=workspace)
        if "origin" not in (await run_git("remote", cwd=mirror)).split():
            return
        url = await run_git("remote", "get-url", "origin", cwd=mirror)
        await run_git("remote", "add", "origin", url.strip(), cwd=workspace)

    async def checkout(self, workspace: Path) -> None:
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
        # The legacy workspace package imports this schema during initialization.
        from octomate.managers.workspaces.mirrors import run_git
        from octomate.managers.workspaces.snapshots import SNAPSHOT_PREFIX

        thread_id, ref = self.thread_id, self.base_ref
        saved = self.snapshot_ref
        # The fork's two endings, and the only place that knows which this is: a
        # thread starting, or one coming back to work a sweep reclaimed the tree
        # of. `resumed` is what separates them in a trace, and the snapshot it
        # came back from is what makes the tree it landed on checkable.
        with volume_logfire.span(
            "volume.checkout",
            thread_id=str(thread_id),
            volume_id=str(self.id),
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
                    self.branch,
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

    async def checkpoint(
        self, backing: HostDirectory, mirror: Path, identity: GitIdentity
    ) -> None:
        """Import a validated snapshot; retain the working tree when capture fails."""
        # The legacy workspace package imports this schema during initialization.
        from octomate.managers.workspaces.mirrors import GitCommandError
        from octomate.managers.workspaces.snapshots import Snapshot

        path = backing.path
        with volume_logfire.span(
            "volume.save",
            thread_id=str(self.thread_id),
            volume_id=str(self.id),
            project=str(self.project_id),
            saved=False,
        ) as span:
            if not path.is_dir():
                return
            try:
                async with (
                    backing.lock,
                    Snapshot.capture(path, identity) as snapshot,
                ):
                    await snapshot.save(
                        mirror,
                        self.snapshot_ref,
                    )
                    span.set_attribute("snapshot", snapshot.commit)
                span.set_attribute("saved", True)
            except (GitCommandError, OSError):
                logger.exception(
                    "the tree for thread %s could not be saved; its work is only in "
                    "%s until a later turn saves it",
                    self.thread_id,
                    path,
                )
