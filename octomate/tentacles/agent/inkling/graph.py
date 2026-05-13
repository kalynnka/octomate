from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

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
from pydantic_ai.output import OutputSpec
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.toolsets import AbstractToolset
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from octomate.managers.conversations import ConversationManager
from octomate.schemas.actions import AgentMessage
from octomate.schemas.conversation import Conversation
from octomate.schemas.messages import ModelResponse
from octomate.tentacles.agent.inkling.resolver import DeferredResolver

logger = logging.getLogger(__name__)

InklingOutput = list[AgentMessage] | DeferredToolRequests


@dataclass
class InklingState:
    conversation: Conversation
    message_history: list[ModelMessage] = field(default_factory=list)


@dataclass
class InklingDeps:
    agent: Agent[None, InklingOutput]
    conversation_manager: ConversationManager
    event_send_stream: ObjectSendStream[
        AgentStreamEvent | AgentRunResultEvent[InklingOutput]
    ] | None = None
    resolver: DeferredResolver | None = None
    output_type: OutputSpec[Any] | None = None
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
        InklingState,
        InklingDeps,
        AgentRunResult[InklingOutput],
    ]
):
    """Entry node: a new user message starts (or continues) a conversation.

    Validates the prompt and drops any trailing deferred-tool ModelResponse
    from the in-memory history before dispatching to RunAgent. pydantic-ai
    rejects `user_prompt` + dangling tool calls (UserError at
    `_agent_graph.py`); a new user message implicitly abandons the prior
    deferral, so we don't show the LLM that response. The DB row is
    preserved for audit/debugging.
    """

    user_prompt: str | Sequence[UserContent] | None

    async def run(self, ctx: GraphRunContext[InklingState, InklingDeps]) -> RunAgent:
        if self.user_prompt is not None and (
            abandoned := drop_trailing_deferral(ctx.state.message_history)
        ) is not None:
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
class ResumeTurn(
    BaseNode[
        InklingState,
        InklingDeps,
        AgentRunResult[InklingOutput],
    ]
):
    """Entry node: continue an in-flight run with resolved deferred-tool results."""

    deferred_results: DeferredToolResults

    async def run(self, ctx: GraphRunContext[InklingState, InklingDeps]) -> RunAgent:
        if not self.deferred_results.calls and not self.deferred_results.approvals:
            raise ValueError(
                "ResumeTurn requires at least one resolved call or approval"
            )
        return RunAgent(deferred_results=self.deferred_results)


@dataclass
class RunAgent(
    BaseNode[
        InklingState,
        InklingDeps,
        AgentRunResult[InklingOutput],
    ]
):
    """Runs one pydantic-ai turn and forwards raw stream events."""

    user_prompt: str | Sequence[UserContent] | None = None
    deferred_results: DeferredToolResults | None = None

    async def run(
        self, ctx: GraphRunContext[InklingState, InklingDeps]
    ) -> ResolveDeferred | End[AgentRunResult[InklingOutput]]:
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

        result: AgentRunResult[InklingOutput] | None = None
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
        ctx: GraphRunContext[InklingState, InklingDeps],
        result: AgentRunResult[InklingOutput],
    ) -> ResolveDeferred | End[AgentRunResult[InklingOutput]]:

        ctx.state.message_history = list(result.all_messages())

        if new_messages := result.new_messages():
            await ctx.deps.conversation_manager.record_agent_run(
                ctx.state.conversation,
                run_id=result.run_id,
                messages=new_messages,
            )

        if isinstance(result.output, DeferredToolRequests) and (
            ctx.deps.resolver is not None
        ):
            return ResolveDeferred(requests=result.output)
        return End(result)


@dataclass
class ResolveDeferred(
    BaseNode[
        InklingState,
        InklingDeps,
        AgentRunResult[InklingOutput],
    ]
):
    requests: DeferredToolRequests

    async def run(self, ctx: GraphRunContext[InklingState, InklingDeps]) -> RunAgent:
        if ctx.deps.resolver is None:
            raise RuntimeError("ResolveDeferred requires an InklingDeps.resolver")
        return RunAgent(deferred_results=await ctx.deps.resolver.resolve(self.requests))


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


async def iter_inkling_graph_events(
    start_node: StartTurn | ResumeTurn,
    *,
    state: InklingState,
    deps: InklingDeps,
) -> AsyncGenerator[
    AgentStreamEvent | AgentRunResultEvent[InklingOutput],
    None,
]:
    send_stream, receive_stream = anyio.create_memory_object_stream[
        AgentStreamEvent | AgentRunResultEvent[InklingOutput]
    ](100)
    graph_deps = replace(deps, event_send_stream=send_stream)

    async def run_graph() -> None:
        async with send_stream:
            await inkling_graph.run(
                start_node,
                state=state,
                deps=graph_deps,
            )

    async with receive_stream:
        async with anyio.create_task_group() as tg:
            tg.start_soon(run_graph)
            async for event in receive_stream:
                yield event


inkling_graph = Graph[
    InklingState,
    InklingDeps,
    AgentRunResult[InklingOutput],
](
    nodes=[StartTurn, ResumeTurn, RunAgent, ResolveDeferred],
    name="inkling",
)
