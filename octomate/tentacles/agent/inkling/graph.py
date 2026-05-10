from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic_ai import Agent
from pydantic_ai.messages import AgentStreamEvent, ModelMessage
from pydantic_ai.run import AgentRunResult, AgentRunResultEvent
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from octomate.schemas.actions import AgentMessage
from octomate.tentacles.agent.inkling.resolver import DeferredResolver

InklingOutput = list[AgentMessage] | DeferredToolRequests
EventSink = Callable[[AgentStreamEvent], Awaitable[None]]


async def noop_sink(_: AgentStreamEvent) -> None:
    pass


@dataclass
class InklingState:
    message_history: list[ModelMessage] = field(default_factory=list)


@dataclass
class InklingDeps:
    agent: Agent[None, InklingOutput]
    resolver: DeferredResolver
    user_prompt: str
    event_sink: EventSink = noop_sink


@dataclass
class RunAgent(BaseNode[InklingState, InklingDeps, list[AgentMessage]]):
    deferred_results: DeferredToolResults | None = None

    async def run(
        self, ctx: GraphRunContext[InklingState, InklingDeps]
    ) -> ResolveDeferred | End[list[AgentMessage]]:
        # user_prompt is only consumed on the first call into the graph
        # (deferred_results=None). Resumed calls after ResolveDeferred carry
        # the deferred outcomes instead and must not re-submit the prompt.
        is_initial = self.deferred_results is None
        result: AgentRunResult[InklingOutput] | None = None

        async for event in ctx.deps.agent.run_stream_events(
            user_prompt=ctx.deps.user_prompt if (is_initial and ctx.deps.user_prompt) else None,
            message_history=ctx.state.message_history or None,
            deferred_tool_results=self.deferred_results,
        ):
            if isinstance(event, AgentRunResultEvent):
                result = event.result
            else:
                await ctx.deps.event_sink(event)

        assert result is not None, "agent.run_stream_events did not yield AgentRunResultEvent"
        ctx.state.message_history = list(result.all_messages())

        if isinstance(result.output, DeferredToolRequests):
            return ResolveDeferred(requests=result.output)
        return End(result.output)


@dataclass
class ResolveDeferred(BaseNode[InklingState, InklingDeps, list[AgentMessage]]):
    requests: DeferredToolRequests

    async def run(
        self, ctx: GraphRunContext[InklingState, InklingDeps]
    ) -> RunAgent:
        results = await ctx.deps.resolver.resolve(self.requests)
        return RunAgent(deferred_results=results)


inkling_graph = Graph[InklingState, InklingDeps, list[AgentMessage]](
    nodes=[RunAgent, ResolveDeferred],
    name="inkling",
)
