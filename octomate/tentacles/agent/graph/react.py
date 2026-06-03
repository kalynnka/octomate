from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, replace
from typing import Generic, TypeAlias, TypeVar

import anyio
import logfire
from anyio.abc import ObjectSendStream
from pydantic import BaseModel, JsonValue
from pydantic_ai import (
    Agent,
    AgentBuiltinTool,
    AgentCapability,
    AgentModelSettings,
    AgentRunResult,
    AgentRunResultEvent,
    AgentStreamEvent,
    RunUsage,
    UsageLimits,
)
from pydantic_ai.agent.abstract import AgentInstructions, AgentMetadata
from pydantic_ai.messages import UserContent
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.output import OutputSpec
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.toolsets import AbstractToolset
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from octomate.managers.conversations import ConversationManager
from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.tentacles.agent.base import AgentOutput, AgentSpecInput
from octomate.tentacles.agent.graph.resolver import DeferredResolver
from octomate.tentacles.agent.graph.suspender import DeferredSuspender

logger = logging.getLogger(__name__)
ReactOutput: TypeAlias = JsonValue | BaseModel | DeferredToolRequests
ReactOutputT = TypeVar("ReactOutputT", bound=AgentOutput)
ReactDepsT = TypeVar("ReactDepsT")


@dataclass
class ReactState:
    """The react graph holds no history of its own — only the identity needed to
    fetch the live conversation from the ConversationManager (the cache + source
    of truth). Every node `ensure()`s the conversation and reads its messages."""

    conversation_key: ConversationKey
    agent_tentacle_id: str


@dataclass
class ReactDeps(Generic[ReactOutputT, ReactDepsT]):
    agent: Agent[ReactDepsT, ReactOutputT]
    conversation_manager: ConversationManager
    agent_deps: ReactDepsT
    event_send_stream: (
        ObjectSendStream[AgentStreamEvent | AgentRunResultEvent[ReactOutputT]] | None
    ) = None
    resolver: DeferredResolver | None = None
    suspender: DeferredSuspender | None = None
    output_type: OutputSpec[ReactOutputT] | None = None
    run_name: str = "react"
    model: Model | KnownModelName | str | None = None
    instructions: AgentInstructions[ReactDepsT] = None
    model_settings: AgentModelSettings[ReactDepsT] | None = None
    usage_limits: UsageLimits | None = None
    usage: RunUsage | None = None
    metadata: AgentMetadata[ReactDepsT] | None = None
    output_retries: int | None = None
    infer_name: bool = True
    toolsets: Sequence[AbstractToolset[ReactDepsT]] | None = None
    builtin_tools: Sequence[AgentBuiltinTool[ReactDepsT]] | None = None
    capabilities: Sequence[AgentCapability[ReactDepsT]] | None = None
    spec: AgentSpecInput | None = None


@dataclass
class StartTurn(
    BaseNode[
        ReactState,
        ReactDeps[ReactOutputT, ReactDepsT],
        AgentRunResult[ReactOutputT],
    ],
    Generic[ReactOutputT, ReactDepsT],
):
    user_prompt: str | Sequence[UserContent] | None

    async def run(
        self, ctx: GraphRunContext[ReactState, ReactDeps[ReactOutputT, ReactDepsT]]
    ) -> RunAgent[ReactOutputT, ReactDepsT]:
        if self.user_prompt is not None:
            conversation = await ctx.deps.conversation_manager.ensure(
                ctx.state.conversation_key,
                agent_tentacle_id=ctx.state.agent_tentacle_id,
            )
            abandoned = await ctx.deps.conversation_manager.drop_trailing_deferral(
                conversation
            )
            if abandoned is not None:
                logger.info(
                    "StartTurn dropped a trailing deferred ModelResponse; the "
                    "new user prompt supersedes the abandoned tool-call request"
                )
        return RunAgent(user_prompt=self.user_prompt)


@dataclass
class ResumeTurn(
    BaseNode[
        ReactState,
        ReactDeps[ReactOutputT, ReactDepsT],
        AgentRunResult[ReactOutputT],
    ],
    Generic[ReactOutputT, ReactDepsT],
):
    deferred_results: DeferredToolResults

    async def run(
        self, ctx: GraphRunContext[ReactState, ReactDeps[ReactOutputT, ReactDepsT]]
    ) -> RunAgent[ReactOutputT, ReactDepsT]:
        if not self.deferred_results.calls and not self.deferred_results.approvals:
            raise ValueError(
                "ResumeTurn requires at least one resolved call or approval"
            )
        return RunAgent(deferred_results=self.deferred_results)


@dataclass
class RunAgent(
    BaseNode[
        ReactState,
        ReactDeps[ReactOutputT, ReactDepsT],
        AgentRunResult[ReactOutputT],
    ],
    Generic[ReactOutputT, ReactDepsT],
):
    user_prompt: str | Sequence[UserContent] | None = None
    deferred_results: DeferredToolResults | None = None

    async def run(
        self, ctx: GraphRunContext[ReactState, ReactDeps[ReactOutputT, ReactDepsT]]
    ) -> ResolveDeferred[ReactOutputT, ReactDepsT] | End[AgentRunResult[ReactOutputT]]:
        with logfire.span(
            "react.agent_run",
            run_name=ctx.deps.run_name,
            streaming=ctx.deps.event_send_stream is not None,
            conversation_key=str(ctx.state.conversation_key),
            resumed=self.deferred_results is not None,
        ) as span:
            conversation = await ctx.deps.conversation_manager.ensure(
                ctx.state.conversation_key,
                agent_tentacle_id=ctx.state.agent_tentacle_id,
            )
            if ctx.deps.event_send_stream is None:
                result = await ctx.deps.agent.run(
                    self.user_prompt,
                    output_type=ctx.deps.output_type,
                    message_history=conversation.messages or None,
                    deferred_tool_results=self.deferred_results,
                    conversation_id=str(conversation.id),
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
                return await self.next_node(ctx, result, conversation, span)

            result: AgentRunResult[ReactOutputT] | None = None
            async with ctx.deps.agent.run_stream_events(
                user_prompt=self.user_prompt,
                output_type=ctx.deps.output_type,
                message_history=conversation.messages or None,
                deferred_tool_results=self.deferred_results,
                conversation_id=str(conversation.id),
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
            return await self.next_node(ctx, result, conversation, span)

    async def next_node(
        self,
        ctx: GraphRunContext[ReactState, ReactDeps[ReactOutputT, ReactDepsT]],
        result: AgentRunResult[ReactOutputT],
        conversation: Conversation,
        span: logfire.LogfireSpan,
    ) -> ResolveDeferred[ReactOutputT, ReactDepsT] | End[AgentRunResult[ReactOutputT]]:
        new_messages = result.new_messages()
        span.set_attribute("react.run_id", result.run_id)
        span.set_attribute(
            "react.deferred", isinstance(result.output, DeferredToolRequests)
        )
        span.set_attribute("react.new_messages", len(new_messages))
        # Recording refreshes the cached conversation, so the next RunAgent's
        # ensure() picks up this turn from the manager — no copy in state.
        if new_messages:
            await ctx.deps.conversation_manager.record_agent_run(
                conversation,
                run_id=result.run_id,
                messages=new_messages,
                name=ctx.deps.run_name,
            )

        if isinstance(result.output, DeferredToolRequests) and (
            ctx.deps.resolver is not None or ctx.deps.suspender is not None
        ):
            logfire.info("react run deferred", run_id=result.run_id)
            return ResolveDeferred(requests=result.output, result=result)
        return End(result)


@dataclass
class ResolveDeferred(
    BaseNode[
        ReactState,
        ReactDeps[ReactOutputT, ReactDepsT],
        AgentRunResult[ReactOutputT],
    ],
    Generic[ReactOutputT, ReactDepsT],
):
    requests: DeferredToolRequests
    result: AgentRunResult[ReactOutputT]

    async def run(
        self, ctx: GraphRunContext[ReactState, ReactDeps[ReactOutputT, ReactDepsT]]
    ) -> RunAgent[ReactOutputT, ReactDepsT] | End[AgentRunResult[ReactOutputT]]:
        if ctx.deps.resolver is not None:
            logfire.info("deferred resolved in-process, looping back to RunAgent")
            return RunAgent(
                deferred_results=await ctx.deps.resolver.resolve(self.requests)
            )
        if ctx.deps.suspender is not None:
            logfire.info("deferred suspended, ending run")
            await ctx.deps.suspender.suspend(self.requests)
            return End(self.result)
        raise RuntimeError(
            "ResolveDeferred requires a react graph resolver or suspender"
        )


REACT_NODES = [StartTurn, ResumeTurn, RunAgent, ResolveDeferred]

react_graph: Graph[
    ReactState,
    ReactDeps[ReactOutput, object],
    AgentRunResult[ReactOutput],
] = Graph(nodes=REACT_NODES, name="react")


async def iter_react_graph_events(
    start_node: StartTurn[ReactOutputT, ReactDepsT]
    | ResumeTurn[ReactOutputT, ReactDepsT],
    *,
    state: ReactState,
    deps: ReactDeps[ReactOutputT, ReactDepsT],
) -> AsyncGenerator[
    AgentStreamEvent | AgentRunResultEvent[ReactOutputT],
    None,
]:
    send_stream, receive_stream = anyio.create_memory_object_stream[
        AgentStreamEvent | AgentRunResultEvent[ReactOutputT]
    ](100)
    graph_deps = replace(deps, event_send_stream=send_stream)
    captured: list[Exception] = []

    async def run_graph() -> None:
        # Capture the graph error instead of letting it escape: a raising child
        # task trips the task group's cancel scope and cancels the consumer
        # mid-event (e.g. blocked on a channel send), which masks the real error
        # and leaves spans unclosed. Closing send_stream ends the consumer loop
        # cleanly; we re-raise below, in the consumer's own frame.
        graph: Graph[
            ReactState,
            ReactDeps[ReactOutputT, ReactDepsT],
            AgentRunResult[ReactOutputT],
        ] = Graph(nodes=REACT_NODES, name="react")
        try:
            async with send_stream:
                await graph.run(
                    start_node,
                    state=state,
                    deps=graph_deps,
                )
        except Exception as exc:
            captured.append(exc)

    async with receive_stream:
        async with anyio.create_task_group() as tg:
            tg.start_soon(run_graph)
            async for event in receive_stream:
                yield event
    for error in captured:
        raise error
