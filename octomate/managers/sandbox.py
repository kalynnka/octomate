"""One persistent local sbx environment per core-resolved registered user."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Literal

from pydantic import UUID7

from octomate.config.sandbox import SandboxConfig, SbxSandboxConfig
from octomate.fibers import FiberError, StdioFiber
from octomate.nerve import (
    Impulse,
    Nerve,
    NerveEvent,
    Publication,
    SandboxTopic,
    UserTopic,
)
from octomate.sandbox import Sandbox
from octomate.sandbox import SandboxError as SandboxError
from octomate.sandbox.sbx import SbxSandbox
from octomate.schemas.sandbox import (
    InvocationScope,
    SandboxAgentReady,
    SandboxFailed,
    SandboxProgress,
    SandboxSnapshot,
)

READY_TIMEOUT = 60.0


@dataclass
class UserSandbox:
    user_id: UUID7
    config: SbxSandboxConfig
    sandbox: Sandbox
    snapshot: SandboxSnapshot
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    fiber: StdioFiber[Impulse[NerveEvent]] | None = None
    connection: asyncio.Task[None] | None = None
    ready: SandboxAgentReady | None = None
    invocations: set[UUID7] = field(default_factory=set)
    needs_cleanup: bool = False  # Preparation began and shutdown has not completed.
    idle: asyncio.TimerHandle | None = None
    stopping: asyncio.Task[None] | None = None


@dataclass
class SandboxManager:
    config: SandboxConfig
    nerve: Nerve
    sandboxes: dict[UUID7, UserSandbox] = field(default_factory=dict, init=False)
    closed: bool = field(default=False, init=False)

    def state(self, user_id: UUID7) -> UserSandbox:
        """Internal lookup; callers must supply a core-authorized owner."""
        if not isinstance(self.config, SbxSandboxConfig):
            raise SandboxError("User sandboxes require sbx configuration")
        if user_id not in self.sandboxes:
            sandbox = SbxSandbox(config=self.config, user_id=user_id)
            self.sandboxes[user_id] = UserSandbox(
                user_id=user_id,
                config=self.config,
                sandbox=sandbox,
                snapshot=SandboxProgress(
                    user_id=user_id,
                    sandbox=sandbox.name,
                    sequence=0,
                    phase="unknown",
                ),
            )
        return self.sandboxes[user_id]

    def snapshot(self, scope: InvocationScope) -> SandboxSnapshot:
        return self.state(scope.user_id).snapshot

    def observe(
        self,
        state: UserSandbox,
        phase: Literal[
            "unknown", "preparing", "starting", "ready", "stopping", "stopped", "failed"
        ],
        *,
        scope: InvocationScope | None = None,
        invocation_id: UUID7 | None = None,
        error: SandboxError | None = None,
    ) -> None:
        if error is not None:
            if phase != "failed":
                raise ValueError("Sandbox errors require the failed phase")
            state.snapshot = SandboxFailed(
                user_id=state.user_id,
                sandbox=state.sandbox.name,
                sequence=state.snapshot.sequence + 1,
                invocation_id=invocation_id,
                thread_id=scope.thread_id if scope else None,
                message=str(error),
            )
        else:
            if phase == "failed":
                raise ValueError("Failed sandbox observations require an error")
            state.snapshot = SandboxProgress(
                user_id=state.user_id,
                sandbox=state.sandbox.name,
                sequence=state.snapshot.sequence + 1,
                invocation_id=invocation_id,
                thread_id=scope.thread_id if scope else None,
                phase=phase,
            )
        self.nerve.deliver(UserTopic(state.user_id), Impulse(state.snapshot))

    async def prepare(
        self, state: UserSandbox, scope: InvocationScope, invocation_id: UUID7
    ) -> None:
        self.observe(state, "preparing", scope=scope, invocation_id=invocation_id)
        await state.sandbox.prepare()
        self.observe(state, "starting", scope=scope, invocation_id=invocation_id)
        stream = await state.sandbox.open()
        topic = SandboxTopic(state.user_id)
        state.fiber = StdioFiber(stream, stream, Publication.adapter(topic))
        async with self.nerve.subscribe(topic) as impulses:
            state.connection = asyncio.create_task(self.connect(topic, state.fiber))
            receiving = asyncio.create_task(impulses.receive())
            try:
                done, _ = await asyncio.wait(
                    {receiving, state.connection},
                    timeout=READY_TIMEOUT,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if receiving not in done or state.connection in done:
                    raise SandboxError(
                        "Sandbox agent readiness failed; check the image and stdio fiber"
                    )
                ready = receiving.result().event
            finally:
                receiving.cancel()
                with suppress(asyncio.CancelledError):
                    await receiving
        if not isinstance(ready, SandboxAgentReady) or (
            ready.user_id,
            ready.image,
            ready.cpus,
            ready.memory_mib,
        ) != (
            state.user_id,
            state.config.image,
            state.config.cpus,
            state.config.memory_mib,
        ):
            raise SandboxError(
                "Existing sandbox owner, image or resource settings differ; no automatic replacement is allowed"
            )
        state.ready = ready
        self.observe(state, "ready", scope=scope, invocation_id=invocation_id)

    async def connect(
        self, topic: SandboxTopic, fiber: StdioFiber[Impulse[NerveEvent]]
    ) -> None:
        """Hold the sandbox's routes until its transport closes or shutdown cancels it."""
        async with (
            self.nerve.subscribe(topic, fiber=fiber),
            self.nerve.publish(topic, fiber=fiber),
        ):
            fiber.start()
            await fiber.wait_closed()

    @asynccontextmanager
    async def acquire(
        self, scope: InvocationScope, invocation_id: UUID7
    ) -> AsyncGenerator[UserSandbox]:
        if scope.mode != "sbx":
            raise SandboxError("Sandbox acquisition requires sbx mode")
        state = self.state(scope.user_id)
        async with state.lock:
            if self.closed:
                raise SandboxError("Sandbox manager is shutting down")
            if invocation_id in state.invocations:
                raise SandboxError("Invocation already holds this sandbox")
            if state.idle is not None:
                state.idle.cancel()
                state.idle = None
            if (state.fiber is not None and state.fiber.closed) or (
                state.connection is not None and state.connection.done()
            ):
                raise SandboxError(
                    "Sandbox agent connection ended; active work is not replayed"
                )
            if state.fiber is None:
                await self.start(state, scope, invocation_id)
            state.invocations.add(invocation_id)
        try:
            yield state
        finally:
            async with state.lock:
                state.invocations.discard(invocation_id)
                if not state.invocations and not self.closed:
                    state.idle = asyncio.get_running_loop().call_later(
                        state.config.idle_timeout, self.schedule_stop, state
                    )

    async def start(
        self, state: UserSandbox, scope: InvocationScope, invocation_id: UUID7
    ) -> None:
        if state.needs_cleanup:
            raise SandboxError(
                "Previous sandbox cleanup is incomplete; stop it before retrying"
            )
        state.needs_cleanup = True
        try:
            await self.prepare(state, scope, invocation_id)
        except BaseException as error:
            failure = (
                error
                if isinstance(error, SandboxError)
                else SandboxError(
                    "Sandbox preparation was interrupted; no agent was dispatched"
                )
            )
            try:
                await self.stop_process(state)
            finally:
                self.observe(
                    state,
                    "failed",
                    scope=scope,
                    invocation_id=invocation_id,
                    error=failure,
                )
            raise

    def schedule_stop(self, state: UserSandbox) -> None:
        state.idle = None
        state.stopping = asyncio.create_task(self.stop(state.user_id))
        state.stopping.add_done_callback(self.stop_finished)

    @staticmethod
    def stop_finished(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            error = task.exception()
            if error is not None and not isinstance(error, SandboxError):
                task.get_loop().call_exception_handler(
                    {
                        "message": "Unexpected sandbox shutdown failure",
                        "exception": error,
                    }
                )

    async def stop_process(self, state: UserSandbox) -> None:
        try:
            if state.connection is not None:
                state.connection.cancel()
                with suppress(asyncio.CancelledError, FiberError):
                    await state.connection
        finally:
            if state.fiber is not None:
                await state.fiber.aclose()
        await state.sandbox.stop()
        state.fiber = None
        state.connection = None
        state.ready = None
        state.needs_cleanup = False

    async def stop(self, user_id: UUID7, *, shutdown: bool = False) -> None:
        state = self.state(user_id)
        async with state.lock:
            if state.invocations and not shutdown:
                return
            if state.idle is not None:
                state.idle.cancel()
                state.idle = None
            if not state.needs_cleanup:
                return
            self.observe(state, "stopping")
            try:
                await self.stop_process(state)
            except SandboxError as error:
                self.observe(state, "failed", error=error)
                raise
            self.observe(state, "stopped")

    async def close(self) -> None:
        self.closed = True
        for state in self.sandboxes.values():
            if state.idle is not None:
                state.idle.cancel()
        results = await asyncio.gather(
            *(self.stop(user_id, shutdown=True) for user_id in tuple(self.sandboxes)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
