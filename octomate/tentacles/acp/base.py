"""Expose the ACP API and run outbound ACP providers through the agent interface."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import acp
from acp import schema
from acp.connection import StreamDirection, StreamEvent
from acp.utils import notify_model, request_model
from anyio import CancelScope
from fastapi import APIRouter
from httpx import URL
from pydantic import UUID7, JsonValue
from pydantic_ai import (
    AgentCapability,
    AgentModelSettings,
    AgentNativeTool,
    AgentRunResult,
    AgentRunResultEvent,
    RunUsage,
    UsageLimits,
)

# External runtimes build the same result state as the existing native harnesses.
from pydantic_ai._agent_graph import GraphAgentState
from pydantic_ai.agent.abstract import (
    AgentInstructions,
    AgentMetadata,
    EventStreamHandler,
    RunOutputDataT,
)
from pydantic_ai.messages import TextContent, ToolCallPart, UserContent
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.output import OutputSpec
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.toolsets import AbstractToolset
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.deferred import DeferredSuspender
from octomate.capabilities.harness.events import (
    ActionBatchAnsweredEvent,
    ActionBatchEvent,
)
from octomate.capabilities.harness.react import ReactEventStream, ReactStreamEvent
from octomate.config.agents import AgentRouteModelName
from octomate.mcp.gateway import CLIENT_HEADER
from octomate.mcp.server import OCTOMATE_MCP_PATH, OCTOMATE_SERVER_NAME
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.segments import TextSegment
from octomate.schemas.user import UserProfile
from octomate.tentacles.acp.adapter import json_value_adapter
from octomate.tentacles.acp.client import AcpProvider, NativeAcpClient
from octomate.tentacles.acp.history import AcpHistory
from octomate.tentacles.agent import AgentSpecInput, AgentTentacle

if TYPE_CHECKING:
    from octomate.base import Octomate
    from octomate.tentacles.acp.broker import AcpBroker


class AcpTentacle(AgentTentacle[str, None]):
    """Own the public connection registry and optional outbound agent execution.

    AcpBroker owns public sessions and dispatch. Outbound runs use the thread and
    conversation supplied by the reflex, with the provider session ID stored on
    that conversation. Each turn holds its managed workspace, provider connection,
    and MCP credentials until the provider has stopped.

    Deployment configuration currently enables only the public API. Supplying
    `provider` programmatically enables outbound execution with the provider's
    default model; provider configuration and model discovery remain future work.
    """

    gateway: bool = False
    in_process: ClassVar[bool] = True
    provider: AcpProvider | None
    clients: dict[UUID7, AcpBroker]  # Public threads attached to a connection.

    def __init__(
        self, id: str, octomate: Octomate, *, provider: AcpProvider | None = None
    ) -> None:
        super().__init__(
            id=id,
            octomate=octomate,
            commands=octomate.commands,
            projects=octomate.projects,
            threads=octomate.threads,
            files=octomate.files,
        )
        self.provider = provider
        self.clients = {}
        self.pendings = {}
        self.conversations = octomate.conversations
        self.workspaces = octomate.workspaces
        self.models = {}

    def routers(self) -> tuple[APIRouter, ...]:
        # Routes depend on this tentacle.
        from octomate.tentacles.acp.routes import build_acp_router

        return (build_acp_router(self),)

    def resolve_model(
        self, model: AgentRouteModelName | None = None
    ) -> AgentRouteModelName | None:
        """Use the outbound provider's default until its model catalog is exposed."""
        if model is not None:
            raise ValueError("Outbound ACP model selection is not available")
        return None

    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: UUID7 | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[UUID7] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: str | None = None,
        conversation_id: UUID7 | None = None,
        interactive: bool = True,
        instructions: AgentInstructions[None] = None,
        deps: None = None,
        model_settings: AgentModelSettings[None] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[None] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[None]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[None]] | None = None,
        event_stream_handler: EventStreamHandler[None] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AgentRunResult[str | RunOutputDataT]:
        """Collect the same agent stream used by the reflex."""
        if event_stream_handler is not None:
            raise ValueError("Use run_stream_events to observe outbound ACP events")
        result: AgentRunResult[str | RunOutputDataT] | None = None
        async with self.run_stream_events(
            user_prompt,
            conversation_address=conversation_address,
            thread_id=thread_id,
            source_thread_address=source_thread_address,
            source_thread_message_ids=source_thread_message_ids,
            run_name=run_name,
            output_type=output_type,
            deferred_tool_results=deferred_tool_results,
            deferred_suspender=deferred_suspender,
            model=model,
            effort=effort,
            conversation_id=conversation_id,
            interactive=interactive,
            instructions=instructions,
            deps=deps,
            model_settings=model_settings,
            usage_limits=usage_limits,
            usage=usage,
            metadata=metadata,
            output_retries=output_retries,
            infer_name=infer_name,
            toolsets=toolsets,
            builtin_tools=builtin_tools,
            capabilities=capabilities,
            spec=spec,
        ) as events:
            async for event in events:
                if isinstance(event, AgentRunResultEvent):
                    result = event.result
        if result is None:
            raise RuntimeError("The ACP stream ended without a result")
        return result

    def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: UUID7 | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[UUID7] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: str | None = None,
        conversation_id: UUID7 | None = None,
        interactive: bool = True,
        instructions: AgentInstructions[None] = None,
        deps: None = None,
        model_settings: AgentModelSettings[None] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[None] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[None]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[None]] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> ReactEventStream[str | RunOutputDataT]:
        """Run the supplied prompt in this agent's conversation and workspace."""
        if (
            any(
                value is not None
                for value in (
                    output_type,
                    deferred_tool_results,
                    model,
                    effort,
                    instructions,
                    model_settings,
                    usage_limits,
                    usage,
                    metadata,
                    output_retries,
                    toolsets,
                    builtin_tools,
                    spec,
                )
            )
            or capabilities
        ):
            raise ValueError("Outbound ACP accepts text prompts with provider defaults")
        return ReactEventStream(
            self.stream(
                user_prompt,
                conversation_address,
                thread_id,
                conversation_id,
                source_thread_address,
                source_thread_message_ids,
                run_name,
                deferred_suspender if interactive else None,
            )
        )

    async def stream(
        self,
        user_prompt: str | Sequence[UserContent] | None,
        address: ChannelAddress,
        thread_id: UUID7 | None,
        conversation_id: UUID7 | None,
        source_address: ChannelAddress | None,
        source_message_ids: Sequence[UUID7] | None,
        run_name: str | None,
        suspender: DeferredSuspender | None,
    ) -> AsyncGenerator[ReactStreamEvent[str]]:
        provider = self.provider
        if provider is None:
            raise ValueError("No outbound ACP provider is configured")
        if thread_id is None:
            raise ValueError("agent run requires a thread_id to own its conversation")
        if isinstance(user_prompt, str):
            text = user_prompt
        else:
            parts: list[str] = []
            for part in user_prompt or []:
                if not isinstance(part, str | TextContent):
                    raise ValueError("Outbound ACP currently accepts only text")
                parts.append(part.content if isinstance(part, TextContent) else part)
            text = "\n".join(parts)
        if not text.strip():
            raise ValueError("ACP requires a non-empty text prompt")
        conversation = await self.resolve_conversation(
            thread_id, conversation_address=address, conversation_id=conversation_id
        )
        sender = await self.octomate.users.profile(
            address.channel_tentacle_id, address.user_id
        )
        if sender is None or sender.user_id is None:
            raise ValueError("Outbound ACP requires a registered user")
        thread = await self.threads.get(thread_id, with_messages=False)
        assert thread is not None
        project = await self.projects.of(thread)
        workspace = self.workspaces.open(thread_id, project)
        updates: asyncio.Queue[
            StreamEvent | ActionBatchEvent | ActionBatchAnsweredEvent | None
        ] = asyncio.Queue()
        session_id: str | None = None

        async def handle(
            method: str, params: JsonValue | None, notification: bool
        ) -> JsonValue | None:
            if notification:
                return None
            if method != "session/request_permission":
                raise acp.RequestError.method_not_found(method)
            request = schema.RequestPermissionRequest.model_validate(params)
            if request.session_id != session_id:
                raise acp.RequestError.invalid_params({"reason": "Unknown session"})
            response = await self.permission(request, suspender, updates)
            return json_value_adapter.validate_python(
                response.model_dump(mode="json", by_alias=True, exclude_none=True)
            )

        async with (
            workspace,
            AsyncExitStack() as credentials,
            NativeAcpClient.connect(
                provider,
                handle,
                schema.InitializeRequest(
                    protocol_version=acp.PROTOCOL_VERSION,
                    client_capabilities=schema.ClientCapabilities(),
                ),
            ) as client,
        ):
            mcp_servers = await credentials.enter_async_context(
                self.mcp_servers(client, sender)
            )
            if conversation.external_id is None:
                native = await client.new_session(
                    schema.NewSessionRequest(
                        cwd=str(workspace.path), mcp_servers=[*mcp_servers]
                    )
                )
                session_id = native.session_id
                await self.conversations.set_external_id(conversation, session_id)
            else:
                capabilities = client.initialization.agent_capabilities
                if capabilities is None or not capabilities.load_session:
                    raise ValueError("The ACP provider cannot reopen this conversation")
                session_id = conversation.external_id
                await request_model(
                    client.connection,
                    acp.AGENT_METHODS["session_load"],
                    schema.LoadSessionRequest(
                        session_id=session_id,
                        cwd=str(workspace.path),
                        mcp_servers=[*mcp_servers],
                    ),
                    schema.LoadSessionResponse,
                )
            request = schema.PromptRequest(
                session_id=session_id, prompt=[acp.text_block(text)]
            )
            async with (
                self.driving(f"{provider.native_id}/{session_id}"),
                aclosing(
                    self.invoke(
                        client,
                        conversation,
                        request,
                        workspace.path,
                        address,
                        sender,
                        source_address,
                        source_message_ids,
                        run_name,
                        updates,
                    )
                ) as events,
            ):
                async for event in events:
                    yield event

    async def permission(
        self,
        request: schema.RequestPermissionRequest,
        suspender: DeferredSuspender | None,
        updates: asyncio.Queue[
            StreamEvent | ActionBatchEvent | ActionBatchAnsweredEvent | None
        ],
    ) -> schema.RequestPermissionResponse:
        """Route provider approvals through the existing live deferred-action flow."""
        if suspender is None:
            return schema.RequestPermissionResponse(
                outcome=schema.DeniedOutcome(outcome="cancelled")
            )
        batch_id = uuid7()
        future: asyncio.Future[DeferredActionBatchResponse] = (
            asyncio.get_running_loop().create_future()
        )
        self.pendings[batch_id] = future
        try:
            batch = await suspender.pause(
                DeferredToolRequests(
                    approvals=[
                        ToolCallPart(
                            tool_name=request.tool_call.title or "ACP tool",
                            tool_call_id=request.tool_call.tool_call_id,
                            args=request.tool_call.model_dump_json(
                                by_alias=True, exclude_none=True
                            ),
                        )
                    ]
                ),
                batch_id=batch_id,
            )
            updates.put_nowait(batch)
            answer = await future
            await self.octomate.deferred_actions.resolve_batch(answer)
            updates.put_nowait(ActionBatchAnsweredEvent.of(answer))
        finally:
            self.pendings.pop(batch_id, None)
        allowed = answer.approvals.get(batch.approvals[0].id, False)
        kind = "allow_once" if allowed else "reject_once"
        option = next(
            (option for option in request.options if option.kind == kind), None
        )
        if option is None:
            return schema.RequestPermissionResponse(
                outcome=schema.DeniedOutcome(outcome="cancelled")
            )
        return schema.RequestPermissionResponse(
            outcome=schema.AllowedOutcome(
                outcome="selected", option_id=option.option_id
            )
        )

    @asynccontextmanager
    async def mcp_servers(
        self, client: NativeAcpClient, sender: UserProfile
    ) -> AsyncGenerator[list[schema.HttpMcpServer]]:
        """Supply the caller's remote Octomate MCP, revoking its key after the turn."""
        capabilities = client.initialization.agent_capabilities
        if (
            capabilities is None
            or capabilities.mcp_capabilities is None
            or not capabilities.mcp_capabilities.http
        ):
            raise ValueError("The ACP provider must support HTTP MCP servers")
        auth = self.octomate.auth
        user_id = sender.user_id
        if auth is None or user_id is None:
            raise ValueError(
                "ACP MCP access requires a registered user and authentication"
            )
        with CancelScope(shield=True):
            issued = await auth.create_api_key(
                user_id,
                name=f"ACP {client.provider.value}",
                scopes=["mcp"],
                expires_at=datetime.now(UTC) + auth.config.runtime_api_key_lifetime,
            )
        config = self.octomate.config
        try:
            yield [
                schema.HttpMcpServer(
                    name=OCTOMATE_SERVER_NAME,
                    type="http",
                    url=str(
                        URL(
                            scheme="http",
                            host=str(config.host),
                            port=config.port,
                            path=OCTOMATE_MCP_PATH,
                        )
                    ),
                    headers=[
                        schema.HttpHeader(
                            name="Authorization",
                            value=f"Bearer {issued.token.get_secret_value()}",
                        ),
                        schema.HttpHeader(
                            name=CLIENT_HEADER, value=client.provider.native_id
                        ),
                    ],
                )
            ]
        finally:
            with CancelScope(shield=True):
                await auth.revoke_api_key(user_id, issued.key.id)

    async def invoke(
        self,
        client: NativeAcpClient,
        conversation: Conversation,
        request: schema.PromptRequest,
        cwd: Path,
        address: ChannelAddress,
        sender: UserProfile,
        source_address: ChannelAddress | None,
        source_message_ids: Sequence[UUID7] | None,
        run_name: str | None,
        updates: asyncio.Queue[
            StreamEvent | ActionBatchEvent | ActionBatchAnsweredEvent | None
        ],
    ) -> AsyncGenerator[ReactStreamEvent[str]]:
        history = AcpHistory(request)
        run_id = str(uuid7())

        def observe(event: StreamEvent) -> None:
            params = event.message.get("params")
            if (
                event.direction == StreamDirection.INCOMING
                and event.message.get("method") == "session/update"
                and isinstance(params, dict)
                and params.get("sessionId") == request.session_id
            ):
                updates.put_nowait(event)

        client.connection.add_observer(observe)
        prompt = asyncio.create_task(
            request_model(
                client.connection,
                acp.AGENT_METHODS["session_prompt"],
                request,
                schema.PromptResponse,
            )
        )
        prompt.add_done_callback(lambda task: updates.put_nowait(None))
        failed = False
        try:
            while (event := await updates.get()) is not None:
                if isinstance(event, ActionBatchEvent | ActionBatchAnsweredEvent):
                    yield event
                    continue
                for wire in history.feed(
                    schema.SessionNotification.model_validate(event.message["params"])
                ):
                    yield wire
            for wire in history.end_part():
                yield wire
            response = await asyncio.shield(prompt)
            assert history.request.metadata is not None
            history.request.metadata["stop_reason"] = response.stop_reason
        except (asyncio.CancelledError, GeneratorExit):
            failed = True
            assert history.request.metadata is not None
            history.request.metadata["stop_reason"] = "cancelled"
            with CancelScope(shield=True):
                await notify_model(
                    client.connection,
                    acp.AGENT_METHODS["session_cancel"],
                    schema.CancelNotification(session_id=request.session_id),
                )
                await prompt
            raise
        except Exception as error:
            failed = True
            assert history.request.metadata is not None
            history.request.metadata["error"] = str(error)
            if not prompt.done():
                await notify_model(
                    client.connection,
                    acp.AGENT_METHODS["session_cancel"],
                    schema.CancelNotification(session_id=request.session_id),
                )
                await prompt
            raise
        finally:
            with CancelScope(shield=True):
                await self.record(
                    client,
                    conversation,
                    history,
                    run_id,
                    cwd,
                    address,
                    sender,
                    source_address,
                    source_message_ids,
                    run_name,
                    updates,
                    failed=failed,
                )
        yield AgentRunResultEvent(
            AgentRunResult(
                output=history.answer,
                _state=GraphAgentState(
                    message_history=history.messages,
                    usage=RunUsage(),
                    run_id=run_id,
                    conversation_id=str(conversation.id),
                ),
            )
        )

    async def record(
        self,
        client: NativeAcpClient,
        conversation: Conversation,
        history: AcpHistory,
        run_id: str,
        cwd: Path,
        address: ChannelAddress,
        sender: UserProfile,
        source_address: ChannelAddress | None,
        source_message_ids: Sequence[UUID7] | None,
        run_name: str | None,
        updates: asyncio.Queue[
            StreamEvent | ActionBatchEvent | ActionBatchAnsweredEvent | None
        ],
        *,
        failed: bool,
    ) -> None:
        """Bind existing inbound messages and preserve partial replies on failure."""
        while not updates.empty():
            pending = updates.get_nowait()
            if isinstance(pending, StreamEvent):
                history.feed(
                    schema.SessionNotification.model_validate(pending.message["params"])
                )
        run = await self.conversations.record_agent_run(
            conversation,
            run_id,
            history.messages,
            name=run_name,
            cwd=cwd,
            external_id=history.prompt.session_id,
            native_id=client.provider.native_id,
        )
        assert run is not None
        if source_message_ids:
            await self.threads.bind_messages(
                list(source_message_ids),
                run.messages[0].id,
                kind="request_source",
                run_id=run.id,
            )
            source_thread = await self.threads.ensure(source_address or address)
            await self.threads.advance_prompt_cursor(
                source_thread, source_message_ids[-1]
            )
        if failed and history.answer:
            reply = await self.threads.record_outbound(
                address,
                agent_tentacle_id=self.id,
                segments=[TextSegment(data={"text": history.answer})],
                sender=sender,
            )
            await self.threads.bind_messages(
                [reply.id],
                run.messages[1].id,
                kind="assistant_reply",
                run_id=run.id,
            )
