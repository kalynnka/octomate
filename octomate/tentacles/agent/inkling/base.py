from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeAlias, overload

from pydantic_ai import (
    Agent,
    AgentBuiltinTool,
    AgentCapability,
    AgentEventStream,
    AgentModelSettings,
    AgentRunResult,
    AgentRunResultEvent,
    AgentStreamEvent,
    RunUsage,
    UsageLimits,
)
from pydantic_ai.agent import EventStreamHandler
from pydantic_ai.agent.abstract import AgentInstructions, AgentMetadata
from pydantic_ai.messages import ModelMessage, UserContent
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.output import OutputSpec
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.result import StreamedRunResult
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.toolsets import AbstractToolset

from octomate.managers.conversations import ConversationManager
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.agent.base import (
    AgentOutput,
    AgentSpecInput,
    AgentTentacle,
)
from octomate.tentacles.agent.graph import (
    DeferredResolver,
    ReactDeps,
    ReactState,
    iter_react_graph_events,
)
from octomate.tentacles.agent.graph.react import ResumeTurn, StartTurn
from octomate.tentacles.agent.inkling.prompts import SYSTEM_PROMPT
from octomate.tentacles.agent.inkling.tools import inkling_toolset

if TYPE_CHECKING:
    from octomate.base import Octomate

InklingOutput: TypeAlias = str | DeferredToolRequests


def build_inkling_agent(
    model_name: str = "gemini-3-flash-preview",
) -> Agent[None, InklingOutput]:
    model_settings: GoogleModelSettings = {"thinking": True}
    model = GoogleModel(
        model_name,
        provider=GoogleProvider(location="global"),
    )
    return Agent(
        model,
        deps_type=type(None),
        output_type=[str, DeferredToolRequests],
        model_settings=model_settings,
        toolsets=[inkling_toolset],
        system_prompt=SYSTEM_PROMPT,
    )


@dataclass
class InklingTentacle(AgentTentacle[InklingOutput, None]):
    """Inkling agent wrapper with pydantic-ai-style run entrypoints."""

    agent: Agent[None, InklingOutput] = field(init=False)
    conversation_manager: ConversationManager = field(init=False)
    deferred_resolver: DeferredResolver | None = None

    def __init__(
        self,
        id: str,
        octomate: Octomate,
        *,
        agent: Agent[None, InklingOutput],
        conversation_manager: ConversationManager | None = None,
        deferred_resolver: DeferredResolver | None = None,
    ) -> None:
        super().__init__(id=id, octomate=octomate)
        self.agent = agent
        self.conversation_manager = conversation_manager or ConversationManager()
        self.deferred_resolver = deferred_resolver

    @overload
    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        output_type: None = None,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
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
        builtin_tools: Sequence[AgentBuiltinTool[None]] | None = None,
        event_stream_handler: EventStreamHandler[None] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AgentRunResult[InklingOutput]: ...

    @overload
    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        output_type: OutputSpec[AgentOutput],
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
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
        builtin_tools: Sequence[AgentBuiltinTool[None]] | None = None,
        event_stream_handler: EventStreamHandler[None] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AgentRunResult[AgentOutput]: ...

    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        output_type: OutputSpec[AgentOutput] | None = None,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
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
        builtin_tools: Sequence[AgentBuiltinTool[None]] | None = None,
        event_stream_handler: EventStreamHandler[None] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AgentRunResult[InklingOutput] | AgentRunResult[AgentOutput]:
        result: AgentRunResult[InklingOutput] | AgentRunResult[AgentOutput] | None = None
        async for event in self.iter_graph_events(
            user_prompt=user_prompt,
            conversation_key=conversation_key,
            run_name=run_name,
            output_type=output_type,
            message_history=message_history,
            deferred_tool_results=deferred_tool_results,
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
    def run_stream(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        output_type: None = None,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
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
        builtin_tools: Sequence[AgentBuiltinTool[None]] | None = None,
        event_stream_handler: EventStreamHandler[None] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AsyncIterator[StreamedRunResult[None, InklingOutput]]: ...

    @overload
    def run_stream(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        output_type: OutputSpec[AgentOutput],
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
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
        builtin_tools: Sequence[AgentBuiltinTool[None]] | None = None,
        event_stream_handler: EventStreamHandler[None] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AsyncIterator[StreamedRunResult[None, AgentOutput]]: ...

    async def run_stream(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        output_type: OutputSpec[AgentOutput] | None = None,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
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
        builtin_tools: Sequence[AgentBuiltinTool[None]] | None = None,
        event_stream_handler: EventStreamHandler[None] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AsyncIterator[
        StreamedRunResult[None, InklingOutput] | StreamedRunResult[None, AgentOutput]
    ]:
        conversation = await self.conversation_manager.ensure(
            conversation_key,
            agent_tentacle_id=self.id,
        )
        async with self.agent.run_stream(
            user_prompt=user_prompt,
            output_type=output_type,
            message_history=message_history,
            deferred_tool_results=deferred_tool_results,
            conversation_id=str(conversation.id),
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
            event_stream_handler=event_stream_handler,
            capabilities=capabilities,
            spec=spec,
        ) as stream:
            yield stream

    @overload
    def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        output_type: None = None,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
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
        builtin_tools: Sequence[AgentBuiltinTool[None]] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AgentEventStream[InklingOutput]: ...

    @overload
    def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        output_type: OutputSpec[AgentOutput],
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
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
        builtin_tools: Sequence[AgentBuiltinTool[None]] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AgentEventStream[AgentOutput]: ...

    def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        output_type: OutputSpec[AgentOutput] | None = None,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
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
        builtin_tools: Sequence[AgentBuiltinTool[None]] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AgentEventStream[InklingOutput] | AgentEventStream[AgentOutput]:
        return AgentEventStream(
            self.iter_graph_events(
                user_prompt=user_prompt,
                conversation_key=conversation_key,
                run_name=run_name,
                output_type=output_type,
                message_history=message_history,
                deferred_tool_results=deferred_tool_results,
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
        conversation_key: ConversationKey,
        run_name: str | None,
        output_type: OutputSpec[AgentOutput] | None,
        message_history: Sequence[ModelMessage] | None,
        deferred_tool_results: DeferredToolResults | None,
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
        builtin_tools: Sequence[AgentBuiltinTool[None]] | None,
        capabilities: Sequence[AgentCapability[None]] | None,
        spec: AgentSpecInput | None,
    ) -> AsyncGenerator[
        AgentStreamEvent | AgentRunResultEvent[InklingOutput | AgentOutput],
        None,
    ]:
        resolved_run_name = run_name or "react"
        react_output_type: OutputSpec[InklingOutput | AgentOutput] = (
            [str, DeferredToolRequests] if output_type is None else output_type
        )
        graph_deps = ReactDeps(
            agent=self.agent,
            conversation_manager=self.conversation_manager,
            agent_deps=deps,
            resolver=self.deferred_resolver,
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
        state = ReactState(
            conversation=await self.conversation_manager.ensure(
                conversation_key,
                agent_tentacle_id=self.id,
            ),
            message_history=list(message_history or []),
        )
        start_node = (
            ResumeTurn(deferred_results=deferred_tool_results)
            if deferred_tool_results is not None
            else StartTurn(user_prompt=user_prompt)
        )
        async for event in iter_react_graph_events(
            start_node,
            state=state,
            deps=graph_deps,
        ):
            yield event
