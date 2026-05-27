from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Generic

import anyio
from anyio.abc import ObjectSendStream
from pydantic_ai import (
    Agent,
    AgentBuiltinTool,
    AgentCapability,
    AgentModelSettings,
    AgentRunResult,
    AgentRunResultEvent,
    AgentSpec,
    AgentStreamEvent,
    RunUsage,
    UsageLimits,
)
from pydantic_ai.agent.abstract import AgentInstructions, AgentMetadata
from pydantic_ai.messages import (
    ModelMessage,
    ToolCallPart,
    UserContent,
)
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.output import OutputDataT, OutputSpec
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.toolsets import AbstractToolset
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from octomate.managers.conversations import ConversationManager
from octomate.schemas.conversation import Conversation
from octomate.schemas.messages import ModelResponse
from octomate.tentacles.agent.graph.resolver import DeferredResolver

logger = logging.getLogger(__name__)


@dataclass
class ReactState:
    conversation: Conversation
    message_history: list[ModelMessage] = field(default_factory=list)


@dataclass
class ReactDeps:
    agent: Agent[None, Any]
    conversation_manager: ConversationManager
    event_send_stream: (
        ObjectSendStream[AgentStreamEvent | AgentRunResultEvent[Any]] | None
    ) = None
    resolver: DeferredResolver | None = None
    output_type: OutputSpec[Any] | None = None
    run_name: str = "react"
    model: Model | KnownModelName | str | None = None
    instructions: AgentInstructions[None] = None
    agent_deps: None = None
    model_settings: AgentModelSettings[None] | None = None
    usage_limits: UsageLimits | None = None
    usage: RunUsage | None = None
    metadata: AgentMetadata[None] | None = None
    output_retries: int | None = None
    infer_name: bool = True
    toolsets: Sequence[AbstractToolset[None]] | None = None
    builtin_tools: Sequence[AgentBuiltinTool[None]] | None = None
    capabilities: Sequence[AgentCapability[None]] | None = None
    spec: dict[str, Any] | AgentSpec | None = None


@dataclass
class StartTurn(
    BaseNode[
        ReactState,
        ReactDeps,
        AgentRunResult[OutputDataT],
    ],
    Generic[OutputDataT],
):
    user_prompt: str | Sequence[UserContent] | None

    async def run(
        self, ctx: GraphRunContext[ReactState, ReactDeps]
    ) -> RunAgent[OutputDataT]:
        if (
            self.user_prompt is not None
            and (abandoned := drop_trailing_deferral(ctx.state.message_history))
            is not None
        ):
            await ctx.deps.conversation_manager.discard_message(abandoned)
            logger.info(
                "StartTurn dropped a trailing deferred ModelResponse; the "
                "new user prompt supersedes the abandoned tool-call request"
            )
        return RunAgent(user_prompt=self.user_prompt)


@dataclass
class ResumeTurn(
    BaseNode[
        ReactState,
        ReactDeps,
        AgentRunResult[OutputDataT],
    ],
    Generic[OutputDataT],
):
    deferred_results: DeferredToolResults

    async def run(
        self, ctx: GraphRunContext[ReactState, ReactDeps]
    ) -> RunAgent[OutputDataT]:
        if not self.deferred_results.calls and not self.deferred_results.approvals:
            raise ValueError(
                "ResumeTurn requires at least one resolved call or approval"
            )
        return RunAgent(deferred_results=self.deferred_results)


@dataclass
class RunAgent(
    BaseNode[
        ReactState,
        ReactDeps,
        AgentRunResult[OutputDataT],
    ],
    Generic[OutputDataT],
):
    user_prompt: str | Sequence[UserContent] | None = None
    deferred_results: DeferredToolResults | None = None

    async def run(
        self, ctx: GraphRunContext[ReactState, ReactDeps]
    ) -> ResolveDeferred[OutputDataT] | End[AgentRunResult[OutputDataT]]:
        if ctx.deps.event_send_stream is None:
            result = await ctx.deps.agent.run(
                self.user_prompt,
                output_type=ctx.deps.output_type,
                message_history=ctx.state.message_history or None,
                deferred_tool_results=self.deferred_results,
                conversation_id=str(ctx.state.conversation.id),
                model=ctx.deps.model,
                instructions=ctx.deps.instructions,
                deps=ctx.deps.agent_deps,
                model_settings=ctx.deps.model_settings,
                usage_limits=ctx.deps.usage_limits,
                usage=ctx.deps.usage,
                metadata=ctx.deps.metadata,
                output_retries=ctx.deps.output_retries,
                infer_name=ctx.deps.infer_name,
                toolsets=ctx.deps.toolsets,
                builtin_tools=ctx.deps.builtin_tools,
                capabilities=ctx.deps.capabilities,
                spec=ctx.deps.spec,
            )
            return await self.next_node(ctx, result)

        result: AgentRunResult[OutputDataT] | None = None
        async with ctx.deps.agent.run_stream_events(
            user_prompt=self.user_prompt,
            output_type=ctx.deps.output_type,
            message_history=ctx.state.message_history or None,
            deferred_tool_results=self.deferred_results,
            conversation_id=str(ctx.state.conversation.id),
            model=ctx.deps.model,
            instructions=ctx.deps.instructions,
            deps=ctx.deps.agent_deps,
            model_settings=ctx.deps.model_settings,
            usage_limits=ctx.deps.usage_limits,
            usage=ctx.deps.usage,
            metadata=ctx.deps.metadata,
            output_retries=ctx.deps.output_retries,
            infer_name=ctx.deps.infer_name,
            toolsets=ctx.deps.toolsets,
            builtin_tools=ctx.deps.builtin_tools,
            capabilities=ctx.deps.capabilities,
            spec=ctx.deps.spec,
        ) as stream:
            async for event in stream:
                if ctx.deps.event_send_stream is not None:
                    await ctx.deps.event_send_stream.send(event)
                if isinstance(event, AgentRunResultEvent):
                    result = event.result

        if result is None:
            raise RuntimeError(
                "agent.run_stream_events did not yield AgentRunResultEvent"
            )
        return await self.next_node(ctx, result)

    async def next_node(
        self,
        ctx: GraphRunContext[ReactState, ReactDeps],
        result: AgentRunResult[OutputDataT],
    ) -> ResolveDeferred[OutputDataT] | End[AgentRunResult[OutputDataT]]:
        ctx.state.message_history = list(result.all_messages())

        if new_messages := result.new_messages():
            await ctx.deps.conversation_manager.record_agent_run(
                ctx.state.conversation,
                run_id=result.run_id,
                messages=new_messages,
                name=ctx.deps.run_name,
            )

        if isinstance(result.output, DeferredToolRequests) and (
            ctx.deps.resolver is not None
        ):
            return ResolveDeferred(requests=result.output)
        return End(result)


@dataclass
class ResolveDeferred(
    BaseNode[
        ReactState,
        ReactDeps,
        AgentRunResult[OutputDataT],
    ],
    Generic[OutputDataT],
):
    requests: DeferredToolRequests

    async def run(
        self, ctx: GraphRunContext[ReactState, ReactDeps]
    ) -> RunAgent[OutputDataT]:
        if ctx.deps.resolver is None:
            raise RuntimeError("ResolveDeferred requires a ReactDeps.resolver")
        return RunAgent(deferred_results=await ctx.deps.resolver.resolve(self.requests))


def drop_trailing_deferral(history: list[ModelMessage]) -> ModelResponse | None:
    if not history:
        return None
    last = history[-1]
    if not isinstance(last, ModelResponse):
        return None
    if not any(isinstance(part, ToolCallPart) for part in last.parts):
        return None
    history.pop()
    return last


async def iter_react_graph_events(
    start_node: StartTurn[OutputDataT] | ResumeTurn[OutputDataT],
    *,
    state: ReactState,
    deps: ReactDeps,
) -> AsyncGenerator[
    AgentStreamEvent | AgentRunResultEvent[OutputDataT],
    None,
]:
    send_stream, receive_stream = anyio.create_memory_object_stream[
        AgentStreamEvent | AgentRunResultEvent[OutputDataT]
    ](100)
    graph_deps = replace(deps, event_send_stream=send_stream)

    async def run_graph() -> None:
        async with send_stream:
            await react_graph.run(
                start_node,
                state=state,
                deps=graph_deps,
            )

    async with receive_stream:
        async with anyio.create_task_group() as tg:
            tg.start_soon(run_graph)
            async for event in receive_stream:
                yield event


react_graph = Graph[
    ReactState,
    ReactDeps,
    AgentRunResult[Any],
](
    nodes=[StartTurn, ResumeTurn, RunAgent, ResolveDeferred],
    name="react",
)
