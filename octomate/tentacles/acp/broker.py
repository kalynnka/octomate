"""Register and control native ACP sessions for one authenticated connection.

Only sessions created through this broker can be prompted or cancelled through
it. Their thread and conversation are durable; the authority to drive them lasts
for this connection. Session loading, reconnects, API routes and transcript
projection are not provided here. Creating a session invents no ledger message
and therefore grants no history access before a real prompt is recorded.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import acp
from acp import schema
from acp.utils import notify_model, request_model

from octomate.managers.conversation import ConversationManager
from octomate.managers.project import ProjectManager
from octomate.managers.thread import ThreadManager
from octomate.schemas.conversation import Conversation
from octomate.schemas.user import UserProfile
from octomate.tentacles.acp.client import NativeAcpClient, NativeAcpSession


@dataclass
class RegisteredAcpSession:
    """A native session's durable home and its live prompt admission state."""

    native: NativeAcpSession
    conversation: Conversation
    prompting: bool = False  # Held until the provider's prompt response settles.


@dataclass
class NativeAcpBroker:
    """The native sessions created by one verified user's adapter connection.

    The caller resolves `sender` through `UserManager.native_profile` after
    authentication and keeps this broker inside `NativeAcpClient.connect`.
    Passing another connection's session ID never attaches that session, even
    when both connections belong to the same user.

    This broker registers identities and controls the provider. It does not
    synthesize model messages from ACP presentation events: transcript ingestion
    remains a separate concern, and no reflex run is dispatched here.
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
        prompt = asyncio.create_task(
            request_model(
                self.client.connection,
                acp.AGENT_METHODS["session_prompt"],
                request,
                schema.PromptResponse,
            )
        )
        try:
            return await asyncio.shield(prompt)
        except asyncio.CancelledError:
            await self.cancel(schema.CancelNotification(session_id=request.session_id))
            await prompt
            raise
        finally:
            session.prompting = False

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
