from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self, TypeAlias, overload

import logfire
from pydantic_ai import (
    AgentCapability,
    AgentModelSettings,
    AgentNativeTool,
    AgentRunResult,
    AgentRunResultEvent,
    RunUsage,
    UsageLimits,
)
from pydantic_ai.agent import EventStreamHandler
from pydantic_ai.agent.abstract import (
    AgentInstructions,
    AgentMetadata,
    RunOutputDataT,
)
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import UserContent
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.output import OutputSpec
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.toolsets import AbstractToolset

from octomate.capabilities.agent import Agent
from octomate.capabilities.deferred import DeferredResolver, DeferredSuspender
from octomate.capabilities.react import (
    ReactDeps,
    ReactEventStream,
    ReactState,
    ReactStreamEvent,
    ResumeTurn,
    StartTurn,
    iter_react_graph_events,
)
from octomate.managers.conversation import ConversationManager
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MessageSegment
from octomate.tentacles.agent.base import (
    AgentSpecInput,
    AgentTentacle,
)
from octomate.tentacles.agent.inkling.prompts import SYSTEM_PROMPT

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)

InklingOutput: TypeAlias = str | list[MessageSegment] | DeferredToolRequests


@dataclass
class InklingTentacle(AgentTentacle[InklingOutput, None]):
    """Inkling agent wrapper with pydantic-ai-style run entrypoints."""

    agent: Agent[None, InklingOutput] = field(init=False)
    conversation_manager: ConversationManager = field(init=False)
    deferred_resolver: DeferredResolver | None = None

    description: str = (
        "General assistant for conversation, questions, writing, analysis, and "
        "coordinating multi-step work."
    )

    _exit_stack: AsyncExitStack = field(init=False)
    toolsets: list[AbstractToolset[None]] = field(init=False)

    def __init__(
        self,
        id: str,
        octomate: Octomate,
        *,
        agent: Agent[None, InklingOutput] | None = None,
        models: Mapping[str, Model | str] | None = None,
        name: str = "octomate-inkling",
        toolsets: Sequence[AbstractToolset[None]] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        conversation_manager: ConversationManager | None = None,
        deferred_resolver: DeferredResolver | None = None,
        description: str | None = None,
    ) -> None:
        super().__init__(id=id, octomate=octomate)
        self.models = dict(models or {})
        if agent is None:
            default_model = next(iter(self.models.values()), None)
            if default_model is None:
                raise ValueError("InklingTentacle requires at least one model")
            agent = Agent(
                default_model,
                deps_type=type(None),
                name=name,
                output_type=[str, list[MessageSegment], DeferredToolRequests],
                toolsets=list(toolsets or []),
                capabilities=list(capabilities or []),
                system_prompt=system_prompt,
            )
        self.agent = agent
        # Default to the project-level manager so every agent conversation shares
        # one source of truth with the thread ledger and the rest of Octomate; an
        # explicit manager still wins.
        self.conversation_manager = conversation_manager or octomate.conversations
        self.deferred_resolver = deferred_resolver
        self.description = description or self.description
        self._exit_stack = AsyncExitStack()
        self.toolsets = list(toolsets or [])

    async def __aenter__(self) -> Self:
        # Enter the wrapped pydantic-ai agent once for the tentacle's lifetime so
        # its MCP toolsets open + `initialize` a single warm session, reused across
        # every react-graph run instead of reconnecting per turn.
        try:
            await self._exit_stack.enter_async_context(self.agent)
        except Exception:
            logger.warning(
                "Agent %s: failed to warm agent/MCP sessions at startup; "
                "runs will reconnect on demand",
                self.id,
                exc_info=True,
            )
            return self
        # Opening the session does not fetch each MCP server's tool list, so the
        # first run would otherwise block on a multi-second `tools/list` before the
        # first token. Prime the per-toolset caches now (concurrently), off the
        # request path. Priming is best-effort too.
        mcp_servers: list[MCPToolset[None]] = []

        def collect(toolset: AbstractToolset[None]) -> None:
            if isinstance(toolset, MCPToolset):
                mcp_servers.append(toolset)

        for toolset in self.toolsets:
            toolset.apply(collect)
        if mcp_servers:
            try:
                with logfire.span(
                    "inkling.warm_mcp_tools",
                    mcp_servers=[server.id for server in mcp_servers],
                ):
                    await asyncio.gather(
                        *(server.list_tools() for server in mcp_servers),
                        return_exceptions=True,
                    )
            except Exception:
                logger.warning(
                    "Agent %s: failed to prime MCP tool lists at startup; "
                    "runs will list on demand",
                    self.id,
                    exc_info=True,
                )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._exit_stack.aclose()

    @overload
    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
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
    ) -> AgentRunResult[InklingOutput]: ...

    @overload
    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT],
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
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
    ) -> AgentRunResult[RunOutputDataT]: ...

    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
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
    ) -> AgentRunResult[InklingOutput | RunOutputDataT]:
        result: AgentRunResult[InklingOutput | RunOutputDataT] | None = None
        async for event in self.iter_graph_events(
            user_prompt=user_prompt,
            conversation_address=conversation_address,
            thread_id=thread_id,
            source_thread_address=source_thread_address,
            source_thread_message_ids=source_thread_message_ids,
            run_name=run_name,
            output_type=output_type,
            deferred_tool_results=deferred_tool_results,
            deferred_suspender=deferred_suspender,
            model=model,
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
        ):
            if isinstance(event, AgentRunResultEvent):
                result = event.result
        if result is None:
            raise RuntimeError("react graph completed without an AgentRunResult")
        return result

    @overload
    def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
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
    ) -> ReactEventStream[InklingOutput]: ...

    @overload
    def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT],
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
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
    ) -> ReactEventStream[RunOutputDataT]: ...

    def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
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
    ) -> ReactEventStream[InklingOutput | RunOutputDataT]:
        return ReactEventStream(
            self.iter_graph_events(
                user_prompt=user_prompt,
                conversation_address=conversation_address,
                thread_id=thread_id,
                source_thread_address=source_thread_address,
                source_thread_message_ids=source_thread_message_ids,
                run_name=run_name,
                output_type=output_type,
                deferred_tool_results=deferred_tool_results,
                deferred_suspender=deferred_suspender,
                model=model,
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
            )
        )

    async def iter_graph_events(
        self,
        *,
        user_prompt: str | Sequence[UserContent] | None,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None,
        source_thread_address: ChannelAddress | None,
        source_thread_message_ids: Sequence[uuid.UUID] | None,
        run_name: str | None,
        output_type: OutputSpec[RunOutputDataT] | None,
        deferred_tool_results: DeferredToolResults | None,
        deferred_suspender: DeferredSuspender | None,
        model: Model | KnownModelName | str | None,
        instructions: AgentInstructions[None],
        deps: None,
        model_settings: AgentModelSettings[None] | None,
        usage_limits: UsageLimits | None,
        usage: RunUsage | None,
        metadata: AgentMetadata[None] | None,
        output_retries: int | None,
        infer_name: bool,
        toolsets: Sequence[AbstractToolset[None]] | None,
        builtin_tools: Sequence[AgentNativeTool[None]] | None,
        capabilities: Sequence[AgentCapability[None]] | None,
        spec: AgentSpecInput | None,
    ) -> AsyncGenerator[
        ReactStreamEvent[InklingOutput | RunOutputDataT],
        None,
    ]:
        resolved_run_name = run_name or "react"
        react_output_type: OutputSpec[InklingOutput | RunOutputDataT] = (
            [str, list[MessageSegment], DeferredToolRequests]
            if output_type is None
            else output_type
        )
        graph_deps = ReactDeps(
            agent=self.agent,
            conversation_manager=self.conversation_manager,
            thread_manager=self.octomate.thread_manager,
            agent_deps=deps,
            resolver=self.deferred_resolver,
            suspender=deferred_suspender,
            output_type=react_output_type,
            run_name=resolved_run_name,
            model=model,
            instructions=instructions,
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
        )
        # The react graph carries only the thread/agent identity; each node fetches
        # the live conversation (and its history) from the ConversationManager, the
        # single source of truth — no message-history copy is threaded here. A
        # conversation is owned by a thread, so the run needs one.
        if thread_id is None:
            raise ValueError("agent run requires a thread_id to own its conversation")
        state = ReactState(
            conversation_address=conversation_address,
            agent_tentacle_id=self.id,
            thread_id=thread_id,
            source_thread_address=source_thread_address,
            source_thread_message_ids=list(source_thread_message_ids or []),
        )
        start_node = (
            ResumeTurn(deferred_results=deferred_tool_results)
            if deferred_tool_results is not None
            else StartTurn(user_prompt=user_prompt)
        )
        with logfire.span(
            "AgentTentacle {agent_id} {run_name} [{conversation_address}]",
            agent_id=self.id,
            run_name=resolved_run_name,
            conversation_address=str(conversation_address),
        ):
            async for event in iter_react_graph_events(
                start_node,
                state=state,
                deps=graph_deps,
            ):
                yield event
