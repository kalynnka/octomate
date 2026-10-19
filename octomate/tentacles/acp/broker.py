"""Register and control native ACP sessions for one authenticated connection.

Only sessions created or authorized for loading through this broker can be driven.
Their thread and conversation are durable; attachment lasts for this connection.
Creating a session invents no ledger message
and therefore grants no history access before a real prompt is recorded.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import acp
from acp import schema
from acp.connection import StreamDirection, StreamEvent
from acp.utils import notify_model, request_model
from uuid_utils.compat import uuid7

from octomate.schemas.awakes import StillAwake, UserMessageSignal
from octomate.schemas.conversation import Conversation
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import ThreadKey
from octomate.schemas.user import UserProfile
from octomate.tentacles.acp.base import AcpTentacle
from octomate.tentacles.acp.client import (
    NativeAcpClient,
    NativeAcpSession,
    validate_workspace,
)
from octomate.tentacles.acp.history import content_value

if TYPE_CHECKING:
    from octomate.base import Octomate


@dataclass
class AcpTurn:
    """The wire request and response retained while its ordinary reflex turn runs."""

    request: schema.PromptRequest
    message: MessageEvent
    response: schema.PromptResponse | None = None


@dataclass
class RegisteredAcpSession:
    """A native session's durable home and the active prompt's wire updates."""

    native: (
        NativeAcpSession[schema.NewSessionResponse]
        | NativeAcpSession[schema.LoadSessionResponse | schema.ResumeSessionResponse]
    )
    conversation: Conversation
    turn: AcpTurn | None = None  # Protocol data only; admission belongs to awakes.
    updates: asyncio.Queue[StreamEvent | None] | None = (
        None  # Wire-ordered input for the active run.
    )


@dataclass
class NativeAcpBroker:
    """The native sessions created by one verified user's adapter connection.

    The caller resolves `sender` through `UserManager.native_profile` after
    authentication and keeps this broker inside `NativeAcpClient.connect`.
    Reopening requires participation in the native thread's own ledger and an
    existing root conversation with the same native identity. A session attaches
    to only one ACP connection in this application at a time.

    Each prompt records one driven run from the ACP conversation. Ledger entries
    grant the caller history access and bind to that run's messages. Stream events
    go through the nerve; provider callbacks still go directly to the ACP caller.
    Each prompt enters the reflex graph; no hook or transcript reader is involved.
    """

    client: NativeAcpClient
    sender: UserProfile  # The verified user's native profile, never request data.
    tentacle: AcpTentacle  # Owns attachment reservations across connections.
    app: Octomate = field(init=False, repr=False)
    sessions: dict[str, RegisteredAcpSession] = field(default_factory=dict, init=False)
    reservations: set[ThreadKey] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        self.app = self.tentacle.octomate
        if self.sender.user_id is None:
            raise ValueError("An ACP broker requires a registered session owner")
        if self.sender.channel_tentacle_id != self.client.provider.native_id:
            raise ValueError("The ACP session owner belongs to another native runtime")
        self.client.connection.add_observer(self.observe)

    def reserve(self, key: ThreadKey) -> None:
        """Reserve a native session without yielding to another connection."""
        if key in self.tentacle.attachments:
            raise acp.RequestError.invalid_request(
                {"reason": "Session is already attached to an ACP connection"}
            )
        self.tentacle.attachments[key] = self
        self.reservations.add(key)

    def close(self) -> None:
        """Release reservations after the connection and its adapter have stopped."""
        for key in self.reservations:
            del self.tentacle.attachments[key]
        self.reservations.clear()
        self.sessions.clear()

    async def authorized_conversation(self, session_id: str) -> Conversation | None:
        """Resolve an accessible native identity without creating or claiming history.

        The ledger is the existing authorization boundary. Empty sessions with no
        recorded participant cannot be reopened after disconnecting.
        """
        thread = await self.app.threads.get(
            ThreadKey(self.client.provider.native_id, "thread", session_id),
            with_messages=False,
            user_id=self.sender.user_id,
        )
        if thread is None:
            return None
        return next(
            (
                conversation
                for conversation in thread.conversations
                if conversation.agent_tentacle_id == self.client.provider.native_id
                and not conversation.subagent_id
                and conversation.external_id == session_id
            ),
            None,
        )

    async def list_sessions(
        self, request: schema.ListSessionsRequest
    ) -> schema.ListSessionsResponse:
        """Filter the provider's page to sessions the caller can access.

        Cursors and directory filters are delegated to the provider. A filtered
        page can be empty and still carry a next cursor. Listing grants no drive
        authority; a session must be loaded or resumed on this connection first.
        """
        capabilities = self.client.initialization.agent_capabilities
        if (
            capabilities is None
            or capabilities.session_capabilities is None
            or capabilities.session_capabilities.list is None
        ):
            raise acp.RequestError.method_not_found("session/list")
        if request.cwd is not None:
            validate_workspace(request.cwd)
        response = await request_model(
            self.client.connection,
            acp.AGENT_METHODS["session_list"],
            request,
            schema.ListSessionsResponse,
        )
        visible: list[schema.SessionInfo] = []
        for item in response.sessions:
            if (
                item.session_id in self.sessions
                or await self.authorized_conversation(item.session_id) is not None
            ):
                visible.append(item)
        return response.model_copy(update={"sessions": visible})

    async def reopen_session(
        self, request: schema.LoadSessionRequest | schema.ResumeSessionRequest
    ) -> schema.LoadSessionResponse | schema.ResumeSessionResponse:
        """Attach an authorized session; loading replays, resuming does not.

        Authorization and reservation precede provider execution. Replay callbacks
        go to the ACP caller, but no prompt recorder is active: loading creates no
        run, ledger message or duplicate nerve event. A failed open releases its
        reservation and never grants prompt authority.
        """
        load = isinstance(request, schema.LoadSessionRequest)
        method = acp.AGENT_METHODS["session_load" if load else "session_resume"]
        capabilities = self.client.initialization.agent_capabilities
        supported = capabilities is not None and (
            capabilities.load_session
            if load
            else capabilities.session_capabilities is not None
            and capabilities.session_capabilities.resume is not None
        )
        if not supported:
            raise acp.RequestError.method_not_found(method)
        validate_workspace(request.cwd, request.additional_directories)
        conversation = await self.authorized_conversation(request.session_id)
        if conversation is None:
            raise acp.RequestError.invalid_params({"reason": "Session is unavailable"})
        key = ThreadKey(self.client.provider.native_id, "thread", request.session_id)
        self.reserve(key)
        try:
            response = await request_model(
                self.client.connection,
                method,
                request,
                schema.LoadSessionResponse if load else schema.ResumeSessionResponse,
            )
            if response is None:
                raise ValueError("ACP session open returned no response")
            native = NativeAcpSession(
                self.client.provider, request.cwd, request.session_id, response
            )
            self.sessions[request.session_id] = RegisteredAcpSession(
                native, conversation
            )
            return response
        except BaseException:
            del self.tentacle.attachments[key]
            self.reservations.remove(key)
            raise

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
        session_id = native.session_id
        project_name = self.app.projects.resolve(Path(native.cwd))
        project = (
            self.app.projects.get(project_name) if project_name is not None else None
        )
        thread = await self.app.threads.ensure(native.key, project=project, new=True)
        conversation = await self.app.conversations.ensure(
            thread.id, agent_tentacle_id=native.provider.native_id, with_history=False
        )
        await self.app.conversations.set_external_id(conversation, session_id)
        self.reserve(native.key)
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
        """Kick the reflex on an attached session and await its settled response.

        The thread's awake task owns prompt admission, including cancellation and
        history settlement. Idle attachments do not hold a reflex awake.
        """
        session = self.sessions.get(request.session_id)
        if session is None:
            raise acp.RequestError.invalid_params(
                {"reason": "Session is unavailable on this connection"}
            )
        address = session.native.key.address(self.sender.channel_user_id)
        turn = AcpTurn(
            request,
            MessageEvent(
                tentacle_id=address.channel_tentacle_id,
                chat_type=address.chat_type,
                chat_id=address.chat_id,
                user_id=address.user_id,
                sender=self.sender,
                message_id=str(uuid7()),
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
        )
        try:
            task = await self.app.kick(
                UserMessageSignal(
                    [turn.message],
                    agent_id=self.tentacle.id,
                )
            )
        except StillAwake as error:
            raise acp.RequestError.invalid_request({"reason": str(error)}) from error
        assert task is not None
        session.turn = turn
        try:
            await task
            if turn.response is None:
                raise RuntimeError("The ACP turn ended without its provider response")
            return turn.response
        finally:
            session.turn = None

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
