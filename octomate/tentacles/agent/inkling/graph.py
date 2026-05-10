from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic_ai import Agent
from pydantic_ai.messages import (
    AgentStreamEvent,
    ModelMessage,
    ToolCallPart,
)
from pydantic_ai.run import AgentRunResult, AgentRunResultEvent
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from octomate.managers.conversations import ConversationManager
from octomate.schemas.actions import AgentMessage
from octomate.schemas.conversation import Conversation
from octomate.schemas.messages import ModelResponse
from octomate.tentacles.agent.inkling.resolver import DeferredResolver

logger = logging.getLogger(__name__)

InklingOutput = list[AgentMessage] | DeferredToolRequests
EventSink = Callable[[AgentStreamEvent], Awaitable[None]]


async def noop_sink(_: AgentStreamEvent) -> None:
    pass


@dataclass
class InklingState:
    message_history: list[ModelMessage] = field(default_factory=list)
    # The conversation being driven. When present alongside
    # `deps.conversation_manager`, nodes persist their newly-produced messages
    # through the manager. None for graph-only callers (tests, embedded use)
    # that don't need durability.
    conversation: Conversation | None = None


@dataclass
class InklingDeps:
    agent: Agent[None, InklingOutput]
    resolver: DeferredResolver
    event_sink: EventSink = noop_sink
    conversation_manager: ConversationManager = ConversationManager()


@dataclass
class StartTurn(BaseNode[InklingState, InklingDeps, list[AgentMessage]]):
    """Entry node: a new user message starts (or continues) a conversation.

    Validates the prompt and drops any trailing deferred-tool ModelResponse
    from the in-memory history before dispatching to RunAgent. pydantic-ai
    rejects `user_prompt` + dangling tool calls (UserError at
    `_agent_graph.py`); a new user message implicitly abandons the prior
    deferral, so we don't show the LLM that response. The DB row is
    preserved for audit/debugging.
    """

    user_prompt: str

    async def run(self, ctx: GraphRunContext[InklingState, InklingDeps]) -> RunAgent:
        if not self.user_prompt:
            raise ValueError("StartTurn requires a non-empty user_prompt")
        if (abandoned := drop_trailing_deferral(ctx.state.message_history)) is not None:
            if ctx.state.conversation is not None:
                # Drop the DB row too — otherwise the abandoned response
                # reappears mid-history on the next reload, past the reach
                # of `drop_trailing_deferral`.
                await ctx.deps.conversation_manager.discard_message(abandoned)
            logger.info(
                "StartTurn dropped a trailing deferred ModelResponse; the "
                "new user prompt supersedes the abandoned tool-call request"
            )
        return RunAgent(user_prompt=self.user_prompt)


@dataclass
class ResumeTurn(BaseNode[InklingState, InklingDeps, list[AgentMessage]]):
    """Entry node: continue an in-flight run with resolved deferred-tool results."""

    deferred_results: DeferredToolResults

    async def run(self, ctx: GraphRunContext[InklingState, InklingDeps]) -> RunAgent:
        if not self.deferred_results.calls and not self.deferred_results.approvals:
            raise ValueError(
                "ResumeTurn requires at least one resolved call or approval"
            )
        return RunAgent(deferred_results=self.deferred_results)


@dataclass
class RunAgent(BaseNode[InklingState, InklingDeps, list[AgentMessage]]):
    """Runs `agent.run_stream_events` once. The entry node guarantees that
    exactly one of `user_prompt` or `deferred_results` is set."""

    user_prompt: str | None = None
    deferred_results: DeferredToolResults | None = None

    async def run(
        self, ctx: GraphRunContext[InklingState, InklingDeps]
    ) -> ResolveDeferred | End[list[AgentMessage]]:
        result: AgentRunResult[InklingOutput] | None = None

        # Pass our conversation's id as pydantic-ai's `conversation_id` so it
        # tags every produced ModelMessage with it natively (no manual
        # backfilling at persist time).
        conversation_id = (
            str(ctx.state.conversation.id)
            if ctx.state.conversation is not None
            else None
        )
        async for event in ctx.deps.agent.run_stream_events(
            user_prompt=self.user_prompt,
            message_history=ctx.state.message_history or None,
            deferred_tool_results=self.deferred_results,
            conversation_id=conversation_id,
        ):
            if isinstance(event, AgentRunResultEvent):
                result = event.result
            else:
                await ctx.deps.event_sink(event)

        assert result is not None, (
            "agent.run_stream_events did not yield AgentRunResultEvent"
        )
        ctx.state.message_history = list(result.all_messages())

        if ctx.state.conversation is not None and (
            new_messages := result.new_messages()
        ):
            await ctx.deps.conversation_manager.record_agent_run(
                ctx.state.conversation,
                run_id=result.run_id,
                messages=new_messages,
            )

        if isinstance(result.output, DeferredToolRequests):
            return ResolveDeferred(requests=result.output)
        return End(result.output)


@dataclass
class ResolveDeferred(BaseNode[InklingState, InklingDeps, list[AgentMessage]]):
    requests: DeferredToolRequests

    async def run(self, ctx: GraphRunContext[InklingState, InklingDeps]) -> RunAgent:
        results = await ctx.deps.resolver.resolve(self.requests)
        return RunAgent(deferred_results=results)


# `state` is NOT a reliable signal for deferred-tool aborts: pydantic-ai sets
# `state='interrupted'` only on stream cancellation, not on a normal deferred
# abort (the stream completes; the agent loop bubbles up DeferredToolRequests
# afterwards). So we detect the deferred-tail by structure — last message is a
# `ModelResponse` with `ToolCallPart`s.
#
# Why the structural check is precise rather than too wide (verified
# empirically against `FunctionModel`-driven runs):
#
#   | Completion mode                  | Trailing message                              | Drops? |
#   |----------------------------------|-----------------------------------------------|--------|
#   | Text final answer                | `ModelResponse(parts=[TextPart, ...])`        | no     |
#   | Multi-step w/ a normal tool      | `ModelResponse(parts=[TextPart])` — the tool  | no     |
#   |                                  | call/return live at [-3]/[-2]                 |        |
#   | Structured output `final_result` | `ModelRequest(parts=[ToolReturnPart(          | no     |
#   |                                  | final_result)])` — pydantic-ai synthesizes    |        |
#   |                                  | the return after the output tool call         |        |
#   | Deferred-tool abort              | `ModelResponse(parts=[ToolCallPart(...)])`    | YES    |
#
# Invariant pydantic-ai maintains: any tool call that was actually executed
# gets a matching `ToolReturnPart` in the next `ModelRequest` before the run
# completes or aborts. The only way a `ToolCallPart` can survive as the
# trailing element is if pydantic-ai chose not to execute it — i.e. deferred.
#
# Edge case to revisit if load-bearing: a single `ModelResponse` mixing a
# `final_result` output-tool call with deferred tool calls. Current behavior
# would drop it; whether that's right depends on whether the structured
# output should be preserved.
def drop_trailing_deferral(history: list[ModelMessage]) -> ModelResponse | None:
    """Pop a trailing ModelResponse that ended on unresolved tool calls.

    Mutates `history` in place — pops the abandoned response and returns
    it so the caller can also clean up the corresponding DB row. Returns
    `None` when nothing needs dropping (history empty, doesn't end in a
    response, or the trailing response carries no tool calls).

    The user is implicitly abandoning the prior deferral by starting a
    new turn; the trailing response must come out of the in-memory
    history because pydantic-ai raises `UserError` if `user_prompt` is
    provided alongside dangling tool calls.
    """
    if not history:
        return None
    last = history[-1]
    if not isinstance(last, ModelResponse):
        return None
    if not any(isinstance(part, ToolCallPart) for part in last.parts):
        return None
    history.pop()
    return last


inkling_graph = Graph[InklingState, InklingDeps, list[AgentMessage]](
    nodes=[StartTurn, ResumeTurn, RunAgent, ResolveDeferred],
    name="inkling",
)
