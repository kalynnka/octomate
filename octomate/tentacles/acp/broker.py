"""Register and control native ACP sessions for one authenticated connection.

Only sessions created through this broker can be prompted, configured or cancelled
through it. Their thread and conversation are durable; the authority to drive them lasts
for this connection. Session loading and reconnects are not provided here.
Creating a session invents no ledger message
and therefore grants no history access before a real prompt is recorded.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import acp
from acp import schema
from acp.connection import StreamDirection, StreamEvent
from acp.utils import notify_model, request_model
from anyio import CancelScope
from anyio.abc import ObjectSendStream
from opentelemetry import trace
from pydantic_ai.messages import EnqueuedMessagesEvent
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.events import (
    RunEndedEvent,
    RunErrorEvent,
    RunStartedEvent,
)
from octomate.managers.conversation import ConversationManager
from octomate.managers.project import ProjectManager
from octomate.managers.thread import ThreadManager
from octomate.nerve import Impulse
from octomate.schemas.conversation import Conversation
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import ThreadMessage
from octomate.schemas.user import UserProfile
from octomate.tentacles.acp.client import NativeAcpClient, NativeAcpSession
from octomate.tentacles.acp.history import AcpHistory, content_value


@dataclass
class RegisteredAcpSession:
    """A native session's durable home and its live prompt admission state."""

    native: NativeAcpSession
    conversation: Conversation
    prompting: bool = False  # Held until the provider's prompt response settles.
    updates: asyncio.Queue[StreamEvent | None] | None = (
        None  # Wire-ordered input for the active run.
    )


@dataclass
class NativeAcpBroker:
    """The native sessions created by one verified user's adapter connection.

    The caller resolves `sender` through `UserManager.native_profile` after
    authentication and keeps this broker inside `NativeAcpClient.connect`.
    Passing another connection's session ID never attaches that session, even
    when both connections belong to the same user.

    Each prompt records one driven run from the ACP conversation. Ledger entries
    grant the caller history access and bind to that run's messages. Stream events
    go through the nerve; provider callbacks still go directly to the ACP caller.
    No hook or transcript reader is involved, and no reflex run is dispatched.
    """

    client: NativeAcpClient
    sender: UserProfile  # The verified user's native profile, never request data.
    threads: ThreadManager
    conversations: ConversationManager
    projects: ProjectManager
    sessions: dict[str, RegisteredAcpSession] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.sender.user_id is None:
            raise ValueError("An ACP broker requires a registered session owner")
        if self.sender.channel_tentacle_id != self.client.provider.native_id:
            raise ValueError("The ACP session owner belongs to another native runtime")
        self.client.connection.add_observer(self.observe)

    def observe(self, event: StreamEvent) -> None:
        """Capture updates before the SDK resolves a later prompt response.

        The SDK schedules callbacks independently. This synchronous observer only
        queues input; validation, persistence and nerve publication are awaited by
        the prompt, so a recording error cannot disappear into an SDK callback.
        """
        if (
            event.direction != StreamDirection.INCOMING
            or event.message.get("method") != "session/update"
        ):
            return
        params = event.message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("sessionId"), str):
            return
        session = self.sessions.get(params["sessionId"])
        if session is not None and session.updates is not None:
            session.updates.put_nowait(event)

    async def new_session(
        self, request: schema.NewSessionRequest
    ) -> schema.NewSessionResponse:
        """Register a newly created native session before returning it to its owner.

        Project attribution uses the local adapter's supplied directory, without
        creating a managed checkout. An existing native identity is refused:
        `session/new` cannot silently attach stored history or another live
        connection. No resume permission is inferred from a provider response.
        """
        native = await self.client.new_session(request)
        session_id = native.response.session_id
        project_name = self.projects.resolve(Path(native.cwd))
        project = self.projects.get(project_name) if project_name is not None else None
        thread = await self.threads.ensure(native.key, project=project, new=True)
        conversation = await self.conversations.ensure(
            thread.id, agent_tentacle_id=native.provider.native_id, with_history=False
        )
        await self.conversations.set_external_id(conversation, session_id)
        self.sessions[session_id] = RegisteredAcpSession(native, conversation)
        return native.response

    async def set_mode(
        self, request: schema.SetSessionModeRequest
    ) -> schema.SetSessionModeResponse:
        """Change an owned session's mode, including while a prompt is running.

        The provider validates available modes and publishes any mode updates.
        """
        if request.session_id not in self.sessions:
            raise acp.RequestError.invalid_params(
                {"reason": "Session is unavailable on this connection"}
            )
        return await request_model(
            self.client.connection,
            acp.AGENT_METHODS["session_set_mode"],
            request,
            schema.SetSessionModeResponse,
        )

    async def set_config_option(
        self,
        request: schema.SetSessionConfigOptionSelectRequest
        | schema.SetSessionConfigOptionBooleanRequest,
    ) -> schema.SetSessionConfigOptionResponse:
        """Change an owned session's setting, including during a prompt.

        The provider validates the choice and returns the complete configuration.
        Its current options may differ from the initial session response.
        """
        if request.session_id not in self.sessions:
            raise acp.RequestError.invalid_params(
                {"reason": "Session is unavailable on this connection"}
            )
        return await request_model(
            self.client.connection,
            acp.AGENT_METHODS["session_set_config_option"],
            request,
            schema.SetSessionConfigOptionResponse,
        )

    async def prompt(self, request: schema.PromptRequest) -> schema.PromptResponse:
        """Forward a prompt only to an idle session owned by this connection.

        Content and metadata cross ACP unchanged, including images and embedded
        context that the selected provider supports. Permissions and updates use
        the client's callback handler while this request remains in flight.

        Cancellation of this coroutine sends ACP cancellation and waits for the
        outstanding response before releasing the session for another prompt.
        """
        session = self.sessions.get(request.session_id)
        if session is None:
            raise acp.RequestError.invalid_params(
                {"reason": "Session is unavailable on this connection"}
            )
        if session.prompting:
            raise acp.RequestError.invalid_request(
                {"reason": "The session already has a prompt in progress"}
            )
        session.prompting = True
        try:
            return await self.run_prompt(session, request)
        finally:
            session.updates = None
            session.prompting = False

    async def run_prompt(
        self, session: RegisteredAcpSession, request: schema.PromptRequest
    ) -> schema.PromptResponse:
        """Record input before execution and drain wire updates before settlement."""
        history = AcpHistory(request)
        run_id = str(uuid7())
        address = session.native.key.address(self.sender.channel_user_id)
        updates: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        inbound = await self.threads.record_inbound(
            MessageEvent(
                tentacle_id=address.channel_tentacle_id,
                chat_id=address.chat_id,
                chat_type=address.chat_type,
                message_id=run_id,
                user_id=address.user_id,
                sender=self.sender,
                segments=[
                    TextSegment(
                        data={
                            "text": value
                            if isinstance(value := content_value(block), str)
                            else f"[{value.media_type}]"
                        }
                    )
                    for block in request.prompt
                ],
                raw=request.model_dump_json(by_alias=True, exclude_none=True),
            ),
            happened_at=history.request.timestamp,
        )
        async with self.threads.nerve.publish(address) as impulses:
            await impulses.send(
                Impulse(RunStartedEvent(address=address, sender=self.sender), address)
            )
            await impulses.send(
                Impulse(
                    EnqueuedMessagesEvent(
                        enqueue_id=str(inbound.id), messages=(history.request,)
                    ),
                    address,
                )
            )

            async def record_updates() -> None:
                while (event := await updates.get()) is not None:
                    notification = schema.SessionNotification.model_validate(
                        event.message["params"]
                    )
                    for wire in history.feed(notification):
                        await impulses.send(Impulse(wire, address))

            session.updates = updates
            recording = asyncio.create_task(record_updates())
            prompt = asyncio.create_task(
                request_model(
                    self.client.connection,
                    acp.AGENT_METHODS["session_prompt"],
                    request,
                    schema.PromptResponse,
                )
            )
            error: Exception | None = None
            try:
                done, _ = await asyncio.wait(
                    (prompt, recording), return_when=asyncio.FIRST_COMPLETED
                )
                if recording in done:
                    await recording
                response = await asyncio.shield(prompt)
                if response is None:
                    raise ValueError("ACP prompt returned no response")
                assert history.request.metadata is not None
                history.request.metadata["stop_reason"] = response.stop_reason
                return response
            except asyncio.CancelledError:
                with CancelScope(shield=True):
                    await self.cancel(
                        schema.CancelNotification(session_id=request.session_id)
                    )
                    response = await prompt
                assert history.request.metadata is not None
                history.request.metadata["stop_reason"] = (
                    response.stop_reason if response is not None else "cancelled"
                )
                raise
            except Exception as failure:
                error = failure
                if not prompt.done():
                    await self.cancel(
                        schema.CancelNotification(session_id=request.session_id)
                    )
                    await prompt
                raise
            finally:
                session.updates = None
                updates.put_nowait(None)
                with CancelScope(shield=True):
                    await recording
                    await self.record_history(
                        session, history, run_id, inbound, impulses, error
                    )

    async def record_history(
        self,
        session: RegisteredAcpSession,
        history: AcpHistory,
        run_id: str,
        inbound: ThreadMessage,
        impulses: ObjectSendStream[Impulse],
        error: Exception | None,
    ) -> None:
        """Commit and bind history before publishing the closing bracket.

        Provider failures retain their input and partial output. A persistence or
        binding failure propagates without publishing a run-ended event.
        """
        address = session.native.key.address(self.sender.channel_user_id)
        for wire in history.end_part():
            await impulses.send(Impulse(wire, address))
        if error is not None:
            if history.request.metadata is not None:
                history.request.metadata["error"] = str(error)
            trace_id = format(
                trace.get_current_span().get_span_context().trace_id, "032x"
            )
            await impulses.send(
                Impulse(
                    RunErrorEvent(message=str(error), trace_id=trace_id),
                    address,
                )
            )
        run = await self.conversations.record_agent_run(
            session.conversation,
            run_id,
            history.messages,
            cwd=Path(session.native.cwd),
            external_id=session.native.response.session_id,
            native_id=session.native.provider.native_id,
        )
        assert run is not None
        await self.threads.bind_messages(
            [inbound.id],
            run.messages[0].id,
            kind="request_source",
            run_id=run.id,
        )
        reply_id = None
        if history.answer:
            reply = await self.threads.record_outbound(
                address,
                agent_tentacle_id=session.native.provider.native_id,
                segments=[TextSegment(data={"text": history.answer})],
                sender=self.sender,
            )
            await self.threads.bind_messages(
                [reply.id], run.messages[1].id, kind="assistant_reply", run_id=run.id
            )
            reply_id = reply.id
        await impulses.send(Impulse(RunEndedEvent(thread_message_id=reply_id), address))

    async def cancel(self, notification: schema.CancelNotification) -> None:
        """Cancel an owned session without waiting on its prompt admission guard.

        Unknown session IDs are a no-op and never reach the provider. The prompt
        remains active until its own response arrives; sending cancellation is
        not proof that the run has stopped.
        """
        if notification.session_id not in self.sessions:
            return
        await notify_model(
            self.client.connection, acp.AGENT_METHODS["session_cancel"], notification
        )
