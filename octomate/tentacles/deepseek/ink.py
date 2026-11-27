"""DSH Remote operations, connection lifecycle and session event routing."""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Self

import anyio
import httpx2
from octomate_protocol.deepseek import ErrResult, RpcError, RpcResult
from pydantic_ai.exceptions import AgentRunError
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from octomate.config.agents.deepseek import DeepseekConfig
from octomate.events import (
    ActionBatchAnsweredEvent,
    ActionBatchEvent,
)
from octomate.habitat import Habitat, HostedHabitat, ManagedHabitat, WorkspaceHabitat
from octomate.tentacles.agent import AgentInk
from octomate.tentacles.deepseek.catalog import (
    DeepseekCommandDescriptor,
    command_descriptors_adapter,
)
from octomate.tentacles.deepseek.client import DeepseekApiClient
from octomate.tentacles.deepseek.process import DeepseekProcess
from octomate.tentacles.deepseek.wire import (
    ApprovalRequestedFrame,
    CommandExecutionValue,
    QuestionRequestedFrame,
    RemoteCancellation,
    RemoteNotification,
    SessionAssistantFrame,
    SessionEventFrame,
    StreamErrorFrame,
)
from octomate.types.json import JsonObject

logger = logging.getLogger(__name__)

type TurnFrame = (
    SessionEventFrame
    | SessionAssistantFrame
    | StreamErrorFrame
    | ActionBatchEvent
    | ActionBatchAnsweredEvent
)

type InteractionHandler = Callable[
    [ApprovalRequestedFrame | QuestionRequestedFrame],
    Coroutine[None, None, RpcResult | None],
]


@dataclass
class DeepseekConnection:
    """Own Remote transport; receive decisions and invalidation callbacks per start."""

    client: DeepseekApiClient
    mux_socket: ClientConnection | None = field(default=None, init=False)
    mux_task: asyncio.Task[None] | None = field(default=None, init=False)
    closing: bool = field(default=False, init=False)
    subscribers: dict[str, asyncio.Queue[TurnFrame]] = field(
        default_factory=dict, init=False
    )
    interaction_tasks: dict[str, asyncio.Task[None]] = field(
        default_factory=dict, init=False
    )

    async def __aenter__(self) -> Self:
        self.closing = False
        await self.client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        self.closing = True
        if self.mux_task is not None:
            self.mux_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.mux_task
            self.mux_task = None
        if self.mux_socket is not None:
            with contextlib.suppress(Exception):
                await self.mux_socket.close()
            self.mux_socket = None
        tasks = list(self.interaction_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.interaction_tasks.clear()
        await self.client.__aexit__(exc_type, exc_value, traceback)

    @property
    def running(self) -> bool:
        return self.mux_task is not None and not self.mux_task.done()

    async def start(
        self,
        *,
        answer_interaction: InteractionHandler,
        invalidate_commands: Callable[[], None],
    ) -> None:
        """Open the event stream after the tentacle starts and authenticates DSH."""
        self.mux_socket = await self.client.open_mux()
        self.mux_task = asyncio.create_task(
            self.pump_mux(
                self.mux_socket,
                answer_interaction=answer_interaction,
                invalidate_commands=invalidate_commands,
            )
        )

    async def subscribe(self, session_id: str) -> asyncio.Queue[TurnFrame]:
        """Register the event queue before asking DSH to follow the session."""
        socket = self.mux_socket
        if socket is None:
            raise AgentRunError("dsh Remote socket is not connected")
        queue: asyncio.Queue[TurnFrame] = asyncio.Queue()
        self.subscribers[session_id] = queue
        try:
            await self.client.follow(socket, session_id)
        except BaseException:
            self.subscribers.pop(session_id, None)
            raise
        return queue

    async def unsubscribe(self, session_id: str) -> None:
        """Release the session queue and stop its native subscription."""
        self.subscribers.pop(session_id, None)
        socket = self.mux_socket
        if socket is None:
            raise AgentRunError("dsh Remote socket is not connected")
        await self.client.unfollow(socket, session_id)

    async def pump_mux(
        self,
        socket: ClientConnection,
        *,
        answer_interaction: InteractionHandler,
        invalidate_commands: Callable[[], None],
    ) -> None:
        """Route session frames and interactions from the shared Remote stream.

        A disconnected stream fails its active subscribers; there is no reconnect.
        """
        try:
            async for rpc_id, frame in self.client.mux_frames(socket):
                if (
                    isinstance(frame, RemoteNotification)
                    and frame.event == "commands/change"
                ):
                    invalidate_commands()
                elif isinstance(
                    frame, SessionEventFrame | SessionAssistantFrame | StreamErrorFrame
                ):
                    self.route_session_frame(frame)
                elif isinstance(frame, ApprovalRequestedFrame | QuestionRequestedFrame):
                    task = asyncio.create_task(
                        self.respond(
                            rpc_id, frame, answer_interaction=answer_interaction
                        )
                    )
                    self.interaction_tasks[rpc_id] = task
                    task.add_done_callback(
                        lambda done, event_id=rpc_id: self.interaction_tasks.pop(
                            event_id, None
                        )
                    )
                elif (
                    isinstance(frame, RemoteCancellation)
                    and (pending := self.interaction_tasks.get(frame.event_id))
                    is not None
                ):
                    pending.cancel()
        except ConnectionClosed:
            pass
        except Exception:
            logger.exception("dsh Remote stream failed")
        finally:
            invalidate_commands()
            if not self.closing:
                self.route_session_frame(
                    StreamErrorFrame(
                        type="stream/error",
                        error=RpcError(
                            code="internal", message="dsh event stream closed"
                        ),
                    )
                )

    def route_session_frame(
        self, frame: SessionEventFrame | SessionAssistantFrame | StreamErrorFrame
    ) -> None:
        """Deliver to the addressed session, or broadcast a connection-wide error."""
        session_id = frame.session_id
        if session_id is None:
            for queue in self.subscribers.values():
                queue.put_nowait(frame)
            return
        queue = self.subscribers.get(session_id)
        if queue is not None:
            queue.put_nowait(frame)

    async def respond(
        self,
        rpc_id: str,
        frame: ApprovalRequestedFrame | QuestionRequestedFrame,
        *,
        answer_interaction: InteractionHandler,
    ) -> None:
        """Send a tentacle's decision and fail its run if DSH rejects the reply."""
        result = await answer_interaction(frame)
        receipt = await self.client.respond(rpc_id, result)
        if receipt.accepted:
            return
        message = f"dsh rejected the {frame.type} response: {receipt.reason}"
        logger.error("session %s: %s", frame.session_id, message)
        self.route_session_frame(
            StreamErrorFrame(
                type="stream/error",
                session_id=frame.session_id,
                error=RpcError(code="interaction-reply-failed", message=message),
            )
        )

    async def list_commands(self, session_id: str) -> list[DeepseekCommandDescriptor]:
        """Inspect the exact receiving agent without creating or prompting a session."""
        result = await self.client.remote("commands/list", {"agentId": session_id})
        if isinstance(result, ErrResult):
            raise AgentRunError(
                f"dsh commands/list failed: {result.error.message} ({result.error.code})"
            )
        return command_descriptors_adapter.validate_python(result.value)

    async def set_permission_mode(self, session_id: str, mode: str) -> None:
        """Switch the session's sandbox and approval preset without opening a turn."""
        execution = await self.execute_command(session_id, f"/permission {mode}")
        if execution is None:
            raise AgentRunError(
                f"dsh has no /permission command, so the run's posture ({mode}) cannot be set"
            )
        if execution.result.kind == "error":
            raise AgentRunError(
                f"dsh refused /permission {mode}: {execution.result.text}"
            )

    async def select_model(
        self,
        session_id: str,
        model: str,
        *,
        default_provider: str | None,
        reasoning_effort: str | None = None,
    ) -> None:
        """Apply the selected provider/model and optional native reasoning effort."""
        provider, separator, model_id = model.partition(":")
        if not separator:
            provider, model_id = default_provider, model
        if provider is None:
            raise ValueError("A DeepSeek model selection must include its provider")
        payload: JsonObject = {
            "sessionId": session_id,
            "provider": provider,
            "model": model_id,
        }
        if reasoning_effort is not None:
            payload["reasoningEffort"] = reasoning_effort
        result = await self.client.remote("session/selectModel", {"request": payload})
        if isinstance(result, ErrResult):
            raise AgentRunError(
                f"dsh session/selectModel failed: {result.error.message} ({result.error.code})"
            )

    async def execute_command(
        self, session_id: str, line: str
    ) -> CommandExecutionValue | None:
        """Submit an exact command line without attachments.

        None means no command matched. Matched success and command errors retain
        their native outcome and lifecycle identity; Remote failures raise.
        """
        result = await self.client.remote(
            "commands/execute",
            {"agentId": session_id, "line": line, "submittedAttachments": []},
        )
        if isinstance(result, ErrResult):
            raise AgentRunError(
                f"dsh commands/execute failed: {result.error.message} ({result.error.code})"
            )
        if result.value is None:
            return None
        return CommandExecutionValue.model_validate(result.value)


@dataclass
class DeepseekInk(AgentInk):
    """Pool of owned DSH Web connections, one per habitat plus core discovery."""

    config: DeepseekConfig
    answer_interaction: InteractionHandler
    invalidate_commands: Callable[[], None]
    connections: dict[str, DeepseekConnection] = field(default_factory=dict, init=False)
    processes: dict[str, DeepseekProcess] = field(default_factory=dict, init=False)
    sessions: dict[str, DeepseekConnection] = field(default_factory=dict, init=False)
    native: bool = True
    started: bool = field(default=False, init=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def __aenter__(self) -> Self:
        self.started = True
        try:
            await self.acquire()
        except BaseException:
            self.started = False
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        self.started = False
        for name in list(self.connections):
            await self.close(name)
        self.sessions.clear()

    async def close(self, name: str) -> None:
        connection = self.connections.pop(name, None)
        process = self.processes.pop(name, None)
        with anyio.CancelScope(shield=True):
            try:
                if connection is not None:
                    await connection.__aexit__()
            finally:
                if process is not None:
                    await process.stop()

    async def acquire(
        self,
        habitat: Habitat | None = None,
        *,
        env: dict[str, str] | None = None,
        session_id: str | None = None,
    ) -> DeepseekConnection:
        """Select core discovery or the runtime owned by the prepared habitat."""
        if not self.started:
            raise RuntimeError(
                "DeepseekTentacle.run requires the tentacle to be entered"
            )
        if habitat is None and session_id is not None:
            connection = self.sessions.get(session_id)
            if connection is None and self.native:
                connection = self.connections.get("core")
            if connection is None or not connection.running:
                raise AgentRunError("DSH session has no active habitat connection")
            return connection
        name = habitat.id if isinstance(habitat, ManagedHabitat) else "core"
        async with self.lock:
            connection = self.connections.get(name)
            if connection is None:
                connection = await self.open(name, habitat, env)
            elif not connection.running:
                raise AgentRunError("DSH event stream is closed")
            if session_id is not None:
                previous = self.sessions.get(session_id)
                if (
                    previous is not None
                    and previous is not connection
                    and previous.running
                ):
                    raise ValueError("DSH session is already open in another habitat")
                self.sessions[session_id] = connection
            return connection

    async def open(
        self, name: str, habitat: Habitat | None, env: dict[str, str] | None
    ) -> DeepseekConnection:
        if isinstance(habitat, ManagedHabitat):
            if env is None:
                raise ValueError("A DSH habitat connection requires its environment")
        process = DeepseekProcess(
            executable=self.config.executable,
            port=0 if isinstance(habitat, WorkspaceHabitat) else self.config.port,
            extra_args=list(self.config.extra_args),
            dsh_home=Path(env["DSH_HOME"])
            if isinstance(habitat, ManagedHabitat) and env is not None
            else self.config.dsh_home,
            ready_timeout=self.config.ready_timeout,
            browser_url=self.config.browser_url,
            habitat=habitat
            if isinstance(habitat, HostedHabitat | WorkspaceHabitat)
            else None,
            env=env or {},
        )
        endpoint = await process.start()
        connection = DeepseekConnection(
            DeepseekApiClient(
                base_url=endpoint,
                http_client=httpx2.AsyncClient(base_url=str(endpoint)),
            )
        )
        try:
            await connection.__aenter__()
            if process.launch_token is not None:
                await connection.client.authenticate(process.launch_token)
            if not await connection.client.answering():
                raise RuntimeError(f"dsh reported {endpoint} but does not answer there")

            async def answer(
                frame: ApprovalRequestedFrame | QuestionRequestedFrame,
            ) -> RpcResult | None:
                if self.sessions.get(frame.session_id) is not connection:
                    return None
                return await self.answer_interaction(frame)

            await connection.start(
                answer_interaction=answer,
                invalidate_commands=self.invalidate_commands,
            )
        except BaseException:
            await connection.__aexit__()
            await process.stop()
            raise
        self.connections[name] = connection
        self.processes[name] = process
        if isinstance(habitat, ManagedHabitat):
            habitat.resources.push_async_callback(self.close, name)
        return connection
