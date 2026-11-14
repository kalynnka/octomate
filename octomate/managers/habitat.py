"""Invocation resources and each registered user's persistent habitat."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

import anyio
from pydantic import UUID7
from uuid_utils.compat import uuid7

from octomate.config.habitat import (
    HabitatConfig,
    NativeHabitatConfig,
    SbxHabitatConfig,
)
from octomate.habitat import Habitat, ManagedHabitat, NativeHabitat
from octomate.habitat import HabitatError as HabitatError
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.workspaces import WorkspaceManager
from octomate.nerve import (
    HabitatTopic,
    Impulse,
    Nerve,
    UserTopic,
)
from octomate.schemas.conversation import Conversation
from octomate.schemas.habitat import (
    HabitatFailed,
    HabitatProgress,
    HabitatStatus,
    InvocationScope,
)
from octomate.schemas.project import Project
from octomate.schemas.thread import Thread
from octomate.schemas.volumes import Volume
from octomate.schemas.volumes.storage import Storage
from octomate.schemas.workspaces import Workspace


@dataclass(frozen=True, kw_only=True)
class Invocation:
    """Core-authorized resources, valid only inside the core's owning contexts.

    Core prepares the storage and acquires any habitat before yielding this
    value. Adapters borrow them for the call; core owns their lifetime and the
    cleanup required on completion, failure, or cancellation.
    """

    id: UUID7  # Allocated by core before acquisition, shared with lifecycle events.
    conversation: Conversation  # Resolved by core; the run's history and settings.
    workspace: Workspace  # Prepared volume composition, owned by core for this call.
    habitat: Habitat  # Prepared environment, owned by core for this call.

    def __post_init__(self) -> None:
        if isinstance(self.habitat, ManagedHabitat):
            if self.owner_id is None:
                raise ValueError("Habitat execution requires a registered user")
            if self.habitat.user_id != self.owner_id:
                raise ValueError("Habitat and volume owners do not match")

    @property
    def owner_id(self) -> UUID7 | None:
        return self.workspace.owner_id

    @property
    def storage(self) -> Storage:
        return self.volume.storage

    @property
    def cwd(self) -> Path | PurePosixPath:
        """The harness's working directory; `storage.path` remains host-side."""
        return self.habitat.path(self.volume)

    @property
    def thread_id(self) -> UUID7:
        return self.workspace.thread_id

    @property
    def project(self) -> Project | None:
        return self.workspace.project

    @property
    def volume(self) -> Volume:
        return self.workspace.volume


@dataclass
class Residence:
    user_id: UUID7
    habitat: ManagedHabitat
    status: HabitatStatus
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    invocations: set[UUID7] = field(default_factory=set)
    needs_cleanup: bool = False  # Preparation began and shutdown has not completed.
    idle: asyncio.TimerHandle | None = None
    stopping: asyncio.Task[None] | None = None


@dataclass
class HabitatManager:
    config: HabitatConfig
    nerve: Nerve
    residences: dict[UUID7, Residence] = field(default_factory=dict, init=False)
    closed: bool = field(default=False, init=False)

    @asynccontextmanager
    async def invoke(
        self,
        *,
        workspaces: WorkspaceManager,
        thread: Thread,
        conversation: Conversation,
        owner_id: UUID7 | None,
    ) -> AsyncGenerator[Invocation]:
        """Own one native call's resources for the full consumption of its events."""
        if not isinstance(self.config, NativeHabitatConfig):
            raise HabitatError("Managed habitat dispatch is not yet supported")
        invocation_id = uuid7()
        project = (
            await workspaces.projects.of(thread) if workspaces.projects.roots else None
        )
        habitat = NativeHabitat()
        await habitat.prepare()
        try:
            async with workspaces.open(thread.id, project, owner_id) as workspace:
                yield Invocation(
                    id=invocation_id,
                    conversation=conversation,
                    workspace=workspace,
                    habitat=habitat,
                )
        finally:
            await habitat.stop()

    def residence(self, scope: InvocationScope) -> Residence:
        """Internal lookup; callers must supply a core-authorized owner."""
        if scope.user_id not in self.residences:
            match self.config:
                case SbxHabitatConfig() as config:
                    habitat = SbxHabitat(config=config, user_id=scope.user_id)
                case NativeHabitatConfig():
                    raise HabitatError("Habitat mode is 'native'; no habitat runs")
            self.residences[scope.user_id] = Residence(
                user_id=scope.user_id,
                habitat=habitat,
                status=HabitatProgress(
                    user_id=scope.user_id,
                    habitat=habitat.name,
                    sequence=0,
                    phase="unknown",
                ),
            )
        return self.residences[scope.user_id]

    def status(self, scope: InvocationScope) -> HabitatStatus:
        return self.residence(scope).status

    def report(
        self,
        residence: Residence,
        phase: Literal[
            "unknown", "preparing", "starting", "ready", "stopping", "stopped", "failed"
        ],
        *,
        scope: InvocationScope | None = None,
        invocation_id: UUID7 | None = None,
        error: HabitatError | None = None,
    ) -> None:
        if error is not None:
            if phase != "failed":
                raise ValueError("Habitat errors require the failed phase")
            residence.status = HabitatFailed(
                user_id=residence.user_id,
                habitat=residence.habitat.name,
                sequence=residence.status.sequence + 1,
                invocation_id=invocation_id,
                thread_id=scope.thread_id if scope else None,
                message=str(error),
            )
        else:
            if phase == "failed":
                raise ValueError("Failed habitat reports require an error")
            residence.status = HabitatProgress(
                user_id=residence.user_id,
                habitat=residence.habitat.name,
                sequence=residence.status.sequence + 1,
                invocation_id=invocation_id,
                thread_id=scope.thread_id if scope else None,
                phase=phase,
            )
        self.nerve.deliver(HabitatTopic(residence.user_id), Impulse(residence.status))
        self.nerve.deliver(UserTopic(residence.user_id), Impulse(residence.status))

    @asynccontextmanager
    async def enter(
        self, scope: InvocationScope, invocation_id: UUID7
    ) -> AsyncGenerator[Residence]:
        residence = self.residence(scope)
        async with residence.lock:
            if self.closed:
                raise HabitatError("Habitat manager is shutting down")
            if invocation_id in residence.invocations:
                raise HabitatError("Invocation is already inside this habitat")
            if residence.idle is not None:
                residence.idle.cancel()
                residence.idle = None
            if residence.status.phase != "ready":
                await self.prepare(residence, scope, invocation_id)
            residence.invocations.add(invocation_id)
        try:
            yield residence
        finally:
            async with AsyncExitStack() as cleanup:
                cleanup.enter_context(anyio.CancelScope(shield=True))
                await cleanup.enter_async_context(residence.lock)
                residence.invocations.discard(invocation_id)
                if not residence.invocations and not self.closed:
                    residence.idle = asyncio.get_running_loop().call_later(
                        residence.habitat.idle_timeout, self.idle_out, residence
                    )

    async def prepare(
        self, residence: Residence, scope: InvocationScope, invocation_id: UUID7
    ) -> None:
        if residence.needs_cleanup:
            raise HabitatError(
                "Previous habitat cleanup is incomplete; stop it before retrying"
            )
        residence.needs_cleanup = True
        try:
            self.report(
                residence, "preparing", scope=scope, invocation_id=invocation_id
            )
            await residence.habitat.prepare()
            self.report(residence, "ready", scope=scope, invocation_id=invocation_id)
        except BaseException as error:
            failure = (
                error
                if isinstance(error, HabitatError)
                else HabitatError(
                    "Habitat preparation was interrupted; no agent was dispatched"
                )
            )
            try:
                with anyio.CancelScope(shield=True):
                    await self.halt(residence)
            finally:
                self.report(
                    residence,
                    "failed",
                    scope=scope,
                    invocation_id=invocation_id,
                    error=failure,
                )
            raise

    def idle_out(self, residence: Residence) -> None:
        residence.idle = None
        residence.stopping = asyncio.create_task(self.stop(residence))
        residence.stopping.add_done_callback(self.halted)

    @staticmethod
    def halted(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            error = task.exception()
            if error is not None and not isinstance(error, HabitatError):
                task.get_loop().call_exception_handler(
                    {
                        "message": "Unexpected habitat shutdown failure",
                        "exception": error,
                    }
                )

    async def halt(self, residence: Residence) -> None:
        await residence.habitat.stop()
        residence.needs_cleanup = False

    async def stop(self, residence: Residence, *, shutdown: bool = False) -> None:
        async with residence.lock:
            if residence.invocations and not shutdown:
                return
            if residence.idle is not None:
                residence.idle.cancel()
                residence.idle = None
            if not residence.needs_cleanup:
                return
            self.report(residence, "stopping")
            try:
                await self.halt(residence)
            except HabitatError as error:
                self.report(residence, "failed", error=error)
                raise
            self.report(residence, "stopped")

    async def close(self) -> None:
        self.closed = True
        for residence in self.residences.values():
            if residence.idle is not None:
                residence.idle.cancel()
        results = await asyncio.gather(
            *(
                self.stop(residence, shutdown=True)
                for residence in self.residences.values()
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
