"""One persistent local sbx environment per core-resolved registered user."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

from pydantic import UUID7

from octomate.config.sandbox import SandboxConfig, SbxSandboxConfig
from octomate.nerve import (
    Impulse,
    Nerve,
    SandboxTopic,
    UserTopic,
)
from octomate.sandbox import Sandbox
from octomate.sandbox import SandboxError as SandboxError
from octomate.sandbox.sbx import SbxSandbox
from octomate.schemas.sandbox import (
    InvocationScope,
    SandboxFailed,
    SandboxProgress,
    SandboxSnapshot,
)


@dataclass
class UserSandbox:
    user_id: UUID7
    config: SbxSandboxConfig
    sandbox: Sandbox
    snapshot: SandboxSnapshot
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
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
        self.nerve.deliver(SandboxTopic(state.user_id), Impulse(state.snapshot))
        self.nerve.deliver(UserTopic(state.user_id), Impulse(state.snapshot))

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
            if state.snapshot.phase != "ready":
                await self.prepare(state, scope, invocation_id)
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

    async def prepare(
        self, state: UserSandbox, scope: InvocationScope, invocation_id: UUID7
    ) -> None:
        if state.needs_cleanup:
            raise SandboxError(
                "Previous sandbox cleanup is incomplete; stop it before retrying"
            )
        state.needs_cleanup = True
        try:
            self.observe(state, "preparing", scope=scope, invocation_id=invocation_id)
            await state.sandbox.prepare()
            self.observe(state, "ready", scope=scope, invocation_id=invocation_id)
        except BaseException as error:
            failure = (
                error
                if isinstance(error, SandboxError)
                else SandboxError(
                    "Sandbox preparation was interrupted; no agent was dispatched"
                )
            )
            try:
                await self.stop_runtime(state)
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

    async def stop_runtime(self, state: UserSandbox) -> None:
        await state.sandbox.stop()
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
                await self.stop_runtime(state)
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
