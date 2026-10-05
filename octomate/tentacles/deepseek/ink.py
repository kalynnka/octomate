"""DSH Remote operations, connection lifecycle and session event routing."""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

from octomate_protocol.deepseek import ErrResult, RpcError, RpcResult
from pydantic_ai.exceptions import AgentRunError
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from octomate.capabilities.harness.events import ActionBatchEvent
from octomate.tentacles.deepseek.catalog import (
    DeepseekCommandDescriptor,
    command_descriptors_adapter,
)
from octomate.tentacles.deepseek.client import DeepseekApiClient
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

logger = logging.getLogger(__name__)

type TurnFrame = (
    SessionEventFrame | SessionAssistantFrame | StreamErrorFrame | ActionBatchEvent
)

type InteractionHandler = Callable[
    [ApprovalRequestedFrame | QuestionRequestedFrame],
    Coroutine[None, None, RpcResult | None],
]


@dataclass
class DeepseekInk:
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
        for task in list(self.interaction_tasks.values()):
            task.cancel()
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
