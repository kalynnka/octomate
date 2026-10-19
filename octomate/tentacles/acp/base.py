"""The ACP agent tentacle, its connections and native session attachments."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from pathlib import Path
from typing import TYPE_CHECKING

import acp
from acp import schema
from acp.connection import StreamEvent
from acp.utils import request_model
from anyio import CancelScope
from fastapi import APIRouter
from pydantic import UUID7
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
from pydantic_ai.messages import EnqueuedMessagesEvent, UserContent
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.output import OutputSpec
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai.toolsets import AbstractToolset

from octomate.capabilities.harness.deferred import DeferredSuspender
from octomate.capabilities.harness.react import ReactEventStream, ReactStreamEvent
from octomate.config.agents import AgentRouteModelName
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import ThreadKey, ThreadMessage
from octomate.tentacles.acp.history import AcpHistory
from octomate.tentacles.agent import AgentSpecInput, AgentTentacle

if TYPE_CHECKING:
    from octomate.base import Octomate
    from octomate.tentacles.acp.broker import (
        AcpTurn,
        NativeAcpBroker,
        RegisteredAcpSession,
    )


class AcpTentacle(AgentTentacle[str, None]):
    """Run an admitted ACP turn on its owner's existing provider connection.

    Protocol payloads remain at this boundary. Reflex sees the usual agent stream
    and result, owns run events and reply bindings, and never needs ACP types.
    Settings and permissions stay with the provider and its connected client.
    This tentacle owns attachments, including pending loads and idle sessions;
    the reflex owns active turns. Routes mount when connected, and an adapter
    starts only when a client initializes.
    """

    gateway: bool = False
    attachments: dict[ThreadKey, NativeAcpBroker]

    def __init__(self, id: str, octomate: Octomate) -> None:
        app = octomate
        super().__init__(
            id=id,
            octomate=app,
            commands=app.commands,
            projects=app.projects,
            threads=app.threads,
            files=app.files,
        )
        self.attachments = {}
        self.conversations = app.conversations
        self.workspaces = app.workspaces
        self.models = {}

    def routers(self) -> tuple[APIRouter, ...]:
        # Routes import Octomate and the broker, which depend on this tentacle.
        from octomate.tentacles.acp.routes import acp_router

        return (acp_router,)

    def resolve_model(
        self, model: AgentRouteModelName | None = None
    ) -> AgentRouteModelName | None:
        """Keep model selection on the attached ACP session."""
        if model is not None:
            raise ValueError("Configure the ACP session through its provider")
        return None

    async def resolve_conversation(
        self,
        thread_id: UUID7,
        *,
        conversation_address: ChannelAddress,
        conversation_id: UUID7 | None = None,
    ) -> Conversation:
        """Use the caller's attached native context without changing its owner."""
        key = ThreadKey.from_address(conversation_address)
        owner = self.attachments.get(key)
        if owner is None:
            raise ValueError("The native session is not attached to this ACP tentacle")
        session = owner.sessions.get(key.chat_id)
        if session is None:
            raise ValueError("The native session has not finished attaching")
        if conversation_address.user_id != owner.sender.channel_user_id:
            raise ValueError("The ACP turn belongs to another caller")
        if thread_id != session.conversation.thread_id:
            raise ValueError("The ACP turn belongs to another thread")
        if conversation_id is not None and conversation_id != session.conversation.id:
            raise ValueError("The ACP turn belongs to another conversation")
        return session.conversation

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
        """Collect the same admitted invocation used by the streaming entry."""
        if event_stream_handler is not None:
            raise ValueError("ACP callbacks are handled by the connected client")
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
        """Stream the selected session; runtime options are set through ACP itself."""
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
            or not interactive
        ):
            raise ValueError("Configure the ACP session through its provider")
        return ReactEventStream(
            self.stream(conversation_address, thread_id, conversation_id)
        )

    async def stream(
        self,
        address: ChannelAddress,
        thread_id: UUID7 | None,
        conversation_id: UUID7 | None,
    ) -> AsyncGenerator[ReactStreamEvent[str]]:
        if thread_id is None:
            raise ValueError("agent run requires a thread_id to own its conversation")
        await self.resolve_conversation(
            thread_id, conversation_address=address, conversation_id=conversation_id
        )
        key = ThreadKey.from_address(address)
        owner = self.attachments[key]
        session = owner.sessions[key.chat_id]
        if session.turn is None:
            raise ValueError("The ACP session has no admitted prompt")
        async with (
            self.driving(f"{key.channel_tentacle_id}/{key.chat_id}"),
            aclosing(self.invoke(owner, session, session.turn)) as events,
        ):
            async for event in events:
                yield event

    async def invoke(
        self, owner: NativeAcpBroker, session: RegisteredAcpSession, turn: AcpTurn
    ) -> AsyncGenerator[ReactStreamEvent[str]]:
        history = AcpHistory(turn.request)
        inbound = await self.threads.record_inbound(
            turn.message, happened_at=history.request.timestamp
        )
        updates: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        session.updates = updates
        prompt = asyncio.create_task(
            request_model(
                owner.client.connection,
                acp.AGENT_METHODS["session_prompt"],
                turn.request,
                schema.PromptResponse,
            )
        )
        prompt.add_done_callback(lambda task: updates.put_nowait(None))
        failed = False
        try:
            yield EnqueuedMessagesEvent(
                enqueue_id=str(inbound.id), messages=(history.request,)
            )
            while (event := await updates.get()) is not None:
                for wire in history.feed(
                    schema.SessionNotification.model_validate(event.message["params"])
                ):
                    yield wire
            for wire in history.end_part():
                yield wire
            turn.response = await asyncio.shield(prompt)
            if turn.response is None:
                raise ValueError("ACP prompt returned no response")
            assert history.request.metadata is not None
            history.request.metadata["stop_reason"] = turn.response.stop_reason
        except (asyncio.CancelledError, GeneratorExit):
            failed = True
            assert history.request.metadata is not None
            history.request.metadata["stop_reason"] = "cancelled"
            with CancelScope(shield=True):
                await owner.cancel(
                    schema.CancelNotification(session_id=turn.request.session_id)
                )
                turn.response = await prompt
            raise
        except Exception as error:
            failed = True
            assert history.request.metadata is not None
            history.request.metadata["error"] = str(error)
            if not prompt.done():
                await owner.cancel(
                    schema.CancelNotification(session_id=turn.request.session_id)
                )
                await prompt
            raise
        finally:
            session.updates = None
            while not updates.empty():
                pending = updates.get_nowait()
                if pending is not None:
                    history.feed(
                        schema.SessionNotification.model_validate(
                            pending.message["params"]
                        )
                    )
            with CancelScope(shield=True):
                await self.record(owner, session, turn, history, inbound, failed=failed)
        yield AgentRunResultEvent(
            AgentRunResult(
                output=history.answer,
                _state=GraphAgentState(
                    message_history=history.messages,
                    usage=RunUsage(),
                    run_id=turn.message.message_id,
                    conversation_id=str(session.conversation.id),
                ),
            )
        )

    async def record(
        self,
        owner: NativeAcpBroker,
        session: RegisteredAcpSession,
        turn: AcpTurn,
        history: AcpHistory,
        inbound: ThreadMessage,
        *,
        failed: bool,
    ) -> None:
        """Persist native messages and input bindings, retaining partial failed replies.

        Successful replies are recorded and bound by the shared reflex runtime.
        A failed stream cannot return a result, so its partial reply is retained here.
        """
        run = await self.conversations.record_agent_run(
            session.conversation,
            turn.message.message_id,
            history.messages,
            cwd=Path(session.native.cwd),
            external_id=session.native.session_id,
            native_id=session.native.provider.native_id,
        )
        assert run is not None
        await self.threads.bind_messages(
            [inbound.id], run.messages[0].id, kind="request_source", run_id=run.id
        )
        if failed and history.answer:
            reply = await self.threads.record_outbound(
                session.native.key.address(owner.sender.channel_user_id),
                agent_tentacle_id=self.id,
                segments=[TextSegment(data={"text": history.answer})],
                sender=owner.sender,
            )
            await self.threads.bind_messages(
                [reply.id], run.messages[1].id, kind="assistant_reply", run_id=run.id
            )
