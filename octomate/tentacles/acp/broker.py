"""One ACP agent over Octomate's registered model routes.

Public session IDs are thread UUIDs. Model option values identify a registered
agent and catalog model; provider session IDs remain on their conversations.
Sessions are private to their creating account, including before their first
message. A connection exclusively attaches the sessions it creates or reopens.

The cwd selects a registered project, otherwise the ordinary chat workspace is
used. Additional directories, client tools, and session-supplied MCP servers are
unsupported. Agents use their existing Octomate tools and linked MCP accounts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import acp
from acp import schema
from acp.connection import Connection
from anyio import CancelScope
from anyio.abc import ObjectReceiveStream
from pydantic import UUID7, JsonValue, TypeAdapter
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.events import ActionBatchEvent, ReflexAsleepEvent
from octomate.nerve import Impulse, ThreadTopic
from octomate.schemas.awakes import (
    DeferredActionBatchResponse,
    StillAwake,
    UserMessageSignal,
)
from octomate.schemas.deferred import QuestionAnswer
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import MessageSegment
from octomate.schemas.thread import Thread, ThreadKey
from octomate.schemas.triage import AgentRouteKey
from octomate.schemas.user import UserProfile
from octomate.tentacles.acp.adapter import (
    AcpTranslator,
    AcpUpdate,
    json_value_adapter,
    prompt_segments,
)
from octomate.tentacles.acp.client import AcpProvider

if TYPE_CHECKING:
    from octomate.reflex.state import ReflexGraphResult
    from octomate.tentacles.acp.channel import AcpChannelTentacle


@dataclass
class AcpSession:
    """A public thread's connection and active prompt state."""

    thread: Thread
    settings_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    prompt_scope: CancelScope | None = None
    turn_task: asyncio.Task[ReflexGraphResult] | None = None

    @property
    def selection(self) -> AgentRouteKey:
        """Read the selected agent and model from the thread's recorded handoff."""
        agent_id = self.thread.active_agent_tentacle_id
        if agent_id is None:
            raise ValueError("The session has no selected agent")
        return AgentRouteKey(agent_id, self.thread.active_model)


@dataclass
class AcpBroker:
    """Serve one authenticated connection using kicks and a nerve subscription.

    Permission requests are answered directly with ACP request_permission.
    Questions use negotiated form elicitation; without that capability the prompt
    fails explicitly rather than fabricating answers. Disconnect and cancellation
    settle the graph before releasing session attachments.
    """

    tentacle: AcpChannelTentacle
    sender: UserProfile
    connection: Connection
    initialized: bool = False
    capabilities: schema.ClientCapabilities = field(
        default_factory=lambda: schema.ClientCapabilities()
    )
    sessions: dict[str, AcpSession] = field(default_factory=dict)
    disconnected: bool = False
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def routes(self) -> dict[str, AgentRouteKey]:
        return {
            f"{agent.id}:{model or ''}": AgentRouteKey(agent.id, model)
            for agent in self.tentacle.octomate.agents.values()
            if agent.models
            for model in [None, *agent.models]
        }

    def session(self, session_id: str) -> AcpSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError("Session is not attached to this connection")
        return session

    async def config_options(
        self, session: AcpSession
    ) -> list[schema.SessionConfigOptionSelect | schema.SessionConfigOptionBoolean]:
        options: list[
            schema.SessionConfigOptionSelect | schema.SessionConfigOptionBoolean
        ] = [
            schema.SessionConfigOptionSelect(
                id="model",
                name="Model",
                category="model",
                type="select",
                current_value=f"{session.selection.agent_id}:{session.selection.model or ''}",
                options=[
                    schema.SessionConfigSelectOption(
                        value=value,
                        name=f"{route.agent_id} / {route.model or 'Default'}",
                    )
                    for value, route in self.routes.items()
                ],
            )
        ]
        modes = await self.modes(session)
        if modes is not None:
            options.append(
                schema.SessionConfigOptionSelect(
                    id="mode",
                    name="Permissions",
                    category="mode",
                    type="select",
                    current_value=modes.current_mode_id,
                    options=[
                        schema.SessionConfigSelectOption(
                            value=mode.id, name=mode.name, description=mode.description
                        )
                        for mode in modes.available_modes
                    ],
                )
            )
        return options

    async def handle(
        self, method: str, params: JsonValue | None, notification: bool
    ) -> JsonValue | None:
        """Validate protocol payloads once, then dispatch typed session operations."""
        if notification:
            if method == "session/cancel" and self.initialized:
                request = schema.CancelNotification.model_validate(params)
                session = self.sessions.get(request.session_id)
                if session is not None and session.prompt_scope is not None:
                    session.prompt_scope.cancel()
            return None
        try:
            response = await self.request(method, params)
        except (ValueError, StillAwake) as error:
            raise acp.RequestError.invalid_params({"reason": str(error)}) from error
        return json_value_adapter.validate_python(
            response.model_dump(mode="json", by_alias=True, exclude_none=True)
        )

    async def request(
        self, method: str, params: JsonValue | None
    ) -> (
        schema.InitializeResponse
        | schema.NewSessionResponse
        | schema.LoadSessionResponse
        | schema.ResumeSessionResponse
        | schema.ListSessionsResponse
        | schema.PromptResponse
        | schema.SetSessionConfigOptionResponse
        | schema.SetSessionModeResponse
    ):
        if method == "initialize":
            if self.initialized:
                raise acp.RequestError.invalid_request(
                    {"reason": "Already initialized"}
                )
            request = schema.InitializeRequest.model_validate(params)
            self.capabilities = (
                request.client_capabilities or schema.ClientCapabilities()
            )
            self.initialized = True
            return schema.InitializeResponse(
                protocol_version=acp.PROTOCOL_VERSION,
                agent_info=schema.Implementation(name="octomate", version="1"),
                auth_methods=[],
                agent_capabilities=schema.AgentCapabilities(
                    auth=None,
                    load_session=True,
                    session_capabilities=schema.SessionCapabilities(
                        list=schema.SessionListCapabilities(),
                        resume=schema.SessionResumeCapabilities(),
                    ),
                    mcp_capabilities=schema.McpCapabilities(),
                    prompt_capabilities=schema.PromptCapabilities(),
                ),
            )
        if not self.initialized:
            raise acp.RequestError.invalid_request({"reason": "Initialize first"})
        if method == "session/new":
            return await self.new_session(
                schema.NewSessionRequest.model_validate(params)
            )
        if method in {"session/load", "session/resume"}:
            request_type = (
                schema.LoadSessionRequest
                if method == "session/load"
                else schema.ResumeSessionRequest
            )
            return await self.reopen(request_type.model_validate(params))
        if method == "session/list":
            return await self.list_sessions(
                schema.ListSessionsRequest.model_validate(params)
            )
        if method == "session/prompt":
            return await self.prompt(schema.PromptRequest.model_validate(params))
        if method == "session/set_config_option":
            request = schema.SetSessionConfigOptionSelectRequest.model_validate(
                params, strict=True
            )
            session = self.session(request.session_id)
            if request.config_id == "model" and request.value in self.routes:
                await self.select(session, self.routes[request.value])
            elif request.config_id == "mode":
                await self.set_mode(session, request.value)
            else:
                raise ValueError("Unknown configuration option")
            options = await self.config_options(session)
            await self.update(
                request.session_id,
                schema.ConfigOptionUpdate(
                    session_update="config_option_update", config_options=options
                ),
            )
            return schema.SetSessionConfigOptionResponse(config_options=options)
        if method == "session/set_mode":
            request = schema.SetSessionModeRequest.model_validate(params)
            session = self.session(request.session_id)
            await self.set_mode(session, request.mode_id)
            await self.update(
                request.session_id,
                schema.ConfigOptionUpdate(
                    session_update="config_option_update",
                    config_options=await self.config_options(session),
                ),
            )
            return schema.SetSessionModeResponse()
        raise acp.RequestError.method_not_found(method)

    async def update(
        self,
        session_id: str,
        update: AcpUpdate | schema.UserMessageChunk | schema.ConfigOptionUpdate,
    ) -> None:
        payload = schema.SessionNotification(session_id=session_id, update=update)
        await self.connection.send_notification(
            "session/update",
            json_value_adapter.validate_python(
                payload.model_dump(mode="json", by_alias=True, exclude_none=True)
            ),
        )

    async def new_session(
        self, request: schema.NewSessionRequest
    ) -> schema.NewSessionResponse:
        app = self.tentacle.octomate
        if request.mcp_servers:
            raise ValueError("Session-supplied MCP servers are not supported")
        if not self.routes:
            raise ValueError("No agent models are available")
        cwd, additional = await AcpProvider.resolve_workspace(
            request.cwd, request.additional_directories
        )
        if additional:
            raise ValueError("Server sessions use only their managed workspace")
        project_name = app.projects.resolve(Path(cwd))
        project = app.projects.get(project_name) if project_name else None
        selection = next(iter(self.routes.values()))
        thread = await app.threads.ensure(
            ThreadKey(
                self.tentacle.id, "thread", str(self.sender.user_id), uuid7().hex
            ),
            project=project,
            new=True,
        )
        session = AcpSession(thread)
        await self.select(session, selection)
        self.sessions[str(thread.id)] = session
        self.tentacle.clients[thread.id] = self
        return schema.NewSessionResponse(
            session_id=str(thread.id),
            config_options=await self.config_options(session),
            modes=await self.modes(session),
        )

    async def select(self, session: AcpSession, selection: AgentRouteKey) -> None:
        async with session.settings_lock:
            app = self.tentacle.octomate
            agent = app.agents[selection.agent_id]
            conversation = await agent.resolve_conversation(
                session.thread.id,
                conversation_address=session.thread.key.address(
                    self.sender.channel_user_id
                ),
            )
            if (
                session.thread.active_agent_tentacle_id,
                session.thread.active_model,
            ) != selection:
                await app.threads.record_handoff(
                    session.thread,
                    to_agent_tentacle_id=selection.agent_id,
                    to_model=selection.model,
                    source_agent_tentacle_id=session.thread.active_agent_tentacle_id,
                    target_conversation_id=conversation.id,
                    reason="The ACP caller selected this model.",
                )

    async def set_mode(self, session: AcpSession, mode: str) -> None:
        async with session.settings_lock:
            agent = self.tentacle.octomate.agents[session.selection.agent_id]
            agent.check_permission_mode(mode)
            conversation = await agent.resolve_conversation(
                session.thread.id,
                conversation_address=session.thread.key.address(
                    self.sender.channel_user_id
                ),
            )
            await agent.set_permission_mode(conversation, mode)

    async def modes(self, session: AcpSession) -> schema.SessionModeState | None:
        agent = self.tentacle.octomate.agents[session.selection.agent_id]
        if not agent.permission_modes:
            return None
        conversation = await agent.resolve_conversation(
            session.thread.id,
            conversation_address=session.thread.key.address(
                self.sender.channel_user_id
            ),
        )
        current = conversation.permission_mode or agent.default_permission_mode
        if current is None:
            return None
        return schema.SessionModeState(
            current_mode_id=current,
            available_modes=[
                schema.SessionMode(
                    id=mode.value, name=mode.name, description=mode.description
                )
                for mode in agent.permission_modes
            ],
        )

    async def reopen(
        self, request: schema.LoadSessionRequest | schema.ResumeSessionRequest
    ) -> schema.LoadSessionResponse | schema.ResumeSessionResponse:
        app = self.tentacle.octomate
        if request.mcp_servers:
            raise ValueError("Session-supplied MCP servers are not supported")
        thread_id = TypeAdapter(UUID7).validate_python(request.session_id)
        thread = await app.threads.get(thread_id, with_messages=True)
        if (
            thread is None
            or thread.channel_tentacle_id != self.tentacle.id
            or thread.chat_id != str(self.sender.user_id)
        ):
            raise ValueError("Session is unavailable")
        await AcpProvider.resolve_workspace(request.cwd, request.additional_directories)
        if request.additional_directories:
            raise ValueError("Server sessions use only their managed workspace")
        if thread.id in self.tentacle.clients:
            owner = self.tentacle.clients[thread.id]
            if not owner.disconnected:
                raise ValueError("Session is already attached to an ACP connection")
            await owner.closed.wait()
        session = AcpSession(thread)
        if session.selection not in self.routes.values():
            raise ValueError("The session's selected model is unavailable")
        # No await between checking and claiming the attachment.
        if thread.id in self.tentacle.clients:
            raise ValueError("Session is already attached to an ACP connection")
        self.tentacle.clients[thread.id] = self
        self.sessions[str(thread.id)] = session
        if isinstance(request, schema.LoadSessionRequest):
            for message in thread.messages:
                text = message.message_text or "\n".join(
                    str(segment) for segment in message.segments
                )
                if text:
                    update = (
                        schema.UserMessageChunk(
                            session_update="user_message_chunk",
                            content=acp.text_block(text),
                        )
                        if message.direction == "inbound"
                        else acp.update_agent_message_text(text)
                    )
                    await self.update(str(thread.id), update)
            return schema.LoadSessionResponse(
                config_options=await self.config_options(session),
                modes=await self.modes(session),
            )
        return schema.ResumeSessionResponse(
            config_options=await self.config_options(session),
            modes=await self.modes(session),
        )

    async def list_sessions(
        self, request: schema.ListSessionsRequest
    ) -> schema.ListSessionsResponse:
        app = self.tentacle.octomate
        offset = int(request.cursor or "0")
        if offset < 0:
            raise ValueError("Invalid session cursor")
        cwd = (
            (await AcpProvider.resolve_workspace(request.cwd))[0]
            if request.cwd
            else None
        )
        project_name = (
            app.projects.resolve(Path(cwd))
            if cwd
            and not Path(cwd).is_relative_to(app.workspaces.workspaces_dir.resolve())
            else None
        )
        rows = await app.threads.list_threads(
            self.tentacle.id, chat_id=str(self.sender.user_id), limit=100, offset=offset
        )
        sessions: list[schema.SessionInfo] = []
        for thread in rows:
            project = await app.projects.of(thread)
            workspace = str(app.workspaces.open(thread.id, project).path)
            if (
                cwd
                and cwd != workspace
                and (project is None or project.name != project_name)
            ):
                continue
            sessions.append(
                schema.SessionInfo(
                    session_id=str(thread.id),
                    cwd=workspace,
                    title=thread.title,
                    updated_at=thread.updated_at.isoformat(),
                )
            )
        return schema.ListSessionsResponse(
            sessions=sessions,
            next_cursor=str(offset + len(rows)) if len(rows) == 100 else None,
        )

    async def prompt(self, request: schema.PromptRequest) -> schema.PromptResponse:
        app = self.tentacle.octomate
        session = self.session(request.session_id)
        if session.prompt_scope is not None or session.thread.key in app.awakes:
            raise StillAwake()
        segments = prompt_segments(request)
        if session.selection not in self.routes.values():
            raise ValueError("The selected model is unavailable")
        with CancelScope() as scope:
            session.prompt_scope = scope
            try:
                await self.run_prompt(session, segments)
            finally:
                session.prompt_scope = None
        return schema.PromptResponse(
            stop_reason="cancelled" if scope.cancel_called else "end_turn"
        )

    async def settle(self, session: AcpSession) -> None:
        """Await this connection's graph task even when its caller was cancelled."""
        with CancelScope(shield=True):
            if session.turn_task is not None:
                session.turn_task.cancel()
                await asyncio.gather(session.turn_task, return_exceptions=True)
                session.turn_task = None

    async def run_prompt(
        self, session: AcpSession, segments: list[MessageSegment]
    ) -> None:
        app = self.tentacle.octomate
        event = MessageEvent(
            tentacle_id=self.tentacle.id,
            chat_type="thread",
            chat_id=session.thread.chat_id,
            channel_thread_id=session.thread.channel_thread_id,
            user_id=self.sender.channel_user_id,
            sender=self.sender,
            message_id=uuid7().hex,
            segments=segments,
        )
        translator = AcpTranslator()
        try:
            inbound = await app.threads.record_inbound(event)
            signal: UserMessageSignal | DeferredActionBatchResponse = UserMessageSignal(
                [event],
                trigger_thread_message_id=inbound.id,
            )
            while True:
                response = await self.drive(session, signal, translator)
                if response is None:
                    return
                signal = response
        finally:
            await self.settle(session)

    async def drive(
        self,
        session: AcpSession,
        signal: UserMessageSignal | DeferredActionBatchResponse,
        translator: AcpTranslator,
    ) -> DeferredActionBatchResponse | None:
        app = self.tentacle.octomate
        with app.nerve.subscribe(ThreadTopic(session.thread.key)) as stream:
            task = await app.kick(signal)
            if task is None:
                raise RuntimeError("Expected a new reflex turn")
            session.turn_task = task
            response = await self.read_events(session, stream, translator)
            await task
            session.turn_task = None
        return response

    async def read_events(
        self,
        session: AcpSession,
        stream: ObjectReceiveStream[Impulse],
        translator: AcpTranslator,
    ) -> DeferredActionBatchResponse | None:
        """Translate the turn and answer live actions before awaiting its completion."""
        app = self.tentacle.octomate
        response: DeferredActionBatchResponse | None = None
        async for impulse in stream:
            event = impulse.event
            if isinstance(event, ReflexAsleepEvent):
                return response
            if isinstance(event, ActionBatchEvent):
                answer = await self.answer(str(session.thread.id), event)
                batch = await app.deferred_actions.get_batch(answer.batch_id)
                if batch.response_mode == "live":
                    await app.kick(answer)
                else:
                    response = answer
            for update in translator.translate(event):
                await self.update(str(session.thread.id), update)
        raise RuntimeError("The event stream ended before the turn settled")

    async def answer(
        self, session_id: str, event: ActionBatchEvent
    ) -> DeferredActionBatchResponse:
        response = DeferredActionBatchResponse(
            batch_id=TypeAdapter(UUID7).validate_python(event.batch_id),
            responder_id=self.sender.channel_user_id,
        )
        for approval in event.approvals:
            request = schema.RequestPermissionRequest(
                session_id=session_id,
                tool_call=schema.ToolCallUpdate(
                    tool_call_id=approval.tool_call_id,
                    title=approval.args.title,
                    raw_input=approval.args.args,
                    content=[
                        schema.ContentToolCallContent(
                            type="content",
                            content=acp.text_block(approval.args.description),
                        )
                    ]
                    if approval.args.description
                    else None,
                ),
                options=[
                    schema.PermissionOption(
                        option_id="allow", name="Allow once", kind="allow_once"
                    ),
                    schema.PermissionOption(
                        option_id="deny", name="Deny", kind="reject_once"
                    ),
                ],
            )
            result = schema.RequestPermissionResponse.model_validate(
                await self.connection.send_request(
                    "session/request_permission",
                    json_value_adapter.validate_python(
                        request.model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        )
                    ),
                )
            )
            response.approvals[approval.id] = (
                result.outcome.outcome == "selected"
                and result.outcome.option_id == "allow"
            )
        for question in event.questions:
            elicitation = self.capabilities.elicitation
            if elicitation is None or elicitation.form is None:
                raise ValueError(
                    "This agent asked a question; the client must support form elicitation"
                )
            choices = question.args.get("choices") or []
            description = question.args.get("hint", "")
            if choices:
                description += "\nSuggested answers: " + "; ".join(choices)
            request = schema.CreateFormSessionElicitationRequest(
                session_id=session_id,
                tool_call_id=question.tool_call_id,
                message=question.args["question"],
                mode="form",
                requested_schema=schema.ElicitationSchema(
                    type="object",
                    properties={
                        "answer": schema.ElicitationStringPropertySchema(
                            type="string", description=description
                        )
                    },
                    required=["answer"],
                ),
            )
            result = TypeAdapter(schema.CreateElicitationResponse).validate_python(
                await self.connection.send_request(
                    "elicitation/create",
                    json_value_adapter.validate_python(
                        request.model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        )
                    ),
                )
            )
            if isinstance(result, schema.AcceptElicitationResponse):
                response.answers[question.id] = TypeAdapter(
                    QuestionAnswer
                ).validate_python((result.content or {}).get("answer"))
            else:
                response.answers[question.id] = ""
        return response

    async def close(self) -> None:
        """Cancel active prompts before releasing this connection's attachments."""
        for session in self.sessions.values():
            if session.prompt_scope is not None:
                session.prompt_scope.cancel()
            await self.settle(session)
        for session in self.sessions.values():
            self.tentacle.clients.pop(session.thread.id, None)
        self.sessions.clear()
        self.closed.set()
