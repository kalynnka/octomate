from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator, AsyncIterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import TracebackType
from typing import Any, Generic, TypeVar

import anyio
import logfire
from anyio.abc import ObjectSendStream
from pydantic import DirectoryPath
from pydantic_ai import (
    AgentCapability,
    AgentModelSettings,
    AgentNativeTool,
    AgentRunResult,
    AgentRunResultEvent,
    AgentSpec,
    RunUsage,
    UsageLimits,
)
from pydantic_ai.agent.abstract import AgentInstructions, AgentMetadata, AgentRetries
from pydantic_ai.capabilities import (
    AbstractCapability,
    AgentNode,
    NativeTool,
    NodeResult,
)
from pydantic_ai.messages import AgentStreamEvent, ModelResponse, UserContent
from pydantic_ai.messages import ModelMessage as PydanticModelMessage
from pydantic_ai.models import KnownModelName, Model, ModelRequestContext
from pydantic_ai.output import OutputSpec
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults, RunContext
from pydantic_ai.toolsets import AbstractToolset
from pydantic_graph import (
    BaseNode,
    End,
    Graph,
    GraphBuilder,
    GraphRunContext,
    TypeExpression,
)
from typing_extensions import TypeAliasType

from octomate.capabilities.harness.agent import Agent
from octomate.capabilities.harness.deferred import DeferredSuspender, ResolverChoice
from octomate.capabilities.harness.events import StreamEvents
from octomate.managers.conversation import ConversationManager
from octomate.managers.thread import ThreadManager
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.messages import ModelRequest
from octomate.schemas.runs import AgentRun as PersistedAgentRun
from octomate.telemetry import react_logfire

logger = logging.getLogger(__name__)
# The react graph is generic machinery: the run's output type is whatever the
# builder's agent/output_type produce, so neither type variable is bounded.
ReactOutputT = TypeVar("ReactOutputT")
ReactOutputCoT = TypeVar("ReactOutputCoT", covariant=True)
ReactDepsT = TypeVar("ReactDepsT")

# The events a react run streams: the normalized `StreamEvents` union (Pydantic AI
# passthrough + output/display events + a suspended run's deferred-action batch)
# plus the terminal result.
ReactStreamEvent = TypeAliasType(
    "ReactStreamEvent",
    StreamEvents[ReactOutputT] | AgentRunResultEvent[ReactOutputT],
    type_params=(ReactOutputT,),
)


@dataclass
class ReactState:
    """The react graph holds no history of its own — only the identity needed to
    fetch the live conversation from the ConversationManager (the cache + source
    of truth). Every node `ensure()`s the conversation and reads its messages."""

    conversation_address: ChannelAddress
    agent_tentacle_id: str
    thread_id: uuid.UUID
    # A pre-ensured conversation to run in, by id — the caller that spawned this
    # run chose the context (e.g. a commissioned accomplice's child conversation).
    # None resolves the agent's own (thread, agent) conversation as usual.
    conversation_id: uuid.UUID | None = None
    source_thread_address: ChannelAddress | None = None
    source_thread_message_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass
class RunPersistence:
    conversation_manager: ConversationManager
    thread_manager: ThreadManager | None
    conversation: Conversation
    state: ReactState
    run_name: str
    cwd: Path | None
    binds_prompt_sources: bool

    async def record(
        self,
        run_id: str,
        messages: Sequence[PydanticModelMessage],
    ) -> PersistedAgentRun | None:
        recorded_run = await self.conversation_manager.record_agent_run(
            self.conversation,
            run_id=run_id,
            messages=messages,
            name=self.run_name,
            cwd=self.cwd,
        )
        if not self.state.source_thread_message_ids or not self.binds_prompt_sources:
            return recorded_run
        if recorded_run is None:
            raise RuntimeError("prompt-source bindings require a persisted agent run")
        prompt_request = next(
            (
                message
                for message in recorded_run.messages
                if isinstance(message, ModelRequest) and message.role == "user"
            ),
            None,
        )
        if prompt_request is None:
            raise RuntimeError(
                "prompt-source bindings require a persisted user ModelRequest"
            )
        if self.thread_manager is None:
            raise RuntimeError("prompt-source bindings require a ThreadManager")
        await self.thread_manager.bind_messages(
            self.state.source_thread_message_ids,
            prompt_request.id,
            kind="request_source",
            run_id=recorded_run.id,
        )
        source_thread = await self.thread_manager.ensure(
            self.state.source_thread_address or self.state.conversation_address
        )
        await self.thread_manager.advance_prompt_cursor(
            source_thread,
            self.state.source_thread_message_ids[-1],
        )
        return recorded_run


@dataclass
class PersistRunFailure(
    AbstractCapability[ReactDepsT],
    Generic[ReactDepsT],
):
    persistence: RunPersistence
    previous_message_count: int
    recorded: bool = False

    async def record_failure(self, ctx: RunContext[ReactDepsT]) -> None:
        if self.recorded:
            return
        messages = ctx.messages[self.previous_message_count :]
        if not messages:
            return
        if ctx.run_id is None:
            raise RuntimeError("failed agent run has no run_id")
        await self.persistence.record(ctx.run_id, messages)
        self.recorded = True

    async def on_node_run_error(
        self,
        ctx: RunContext[ReactDepsT],
        *,
        node: AgentNode[ReactDepsT],
        error: Exception,
    ) -> NodeResult[ReactDepsT]:
        await self.record_failure(ctx)
        raise error

    async def on_model_request_error(
        self,
        ctx: RunContext[ReactDepsT],
        *,
        request_context: ModelRequestContext,
        error: Exception,
    ) -> ModelResponse:
        await self.record_failure(ctx)
        raise error


@dataclass
class PersistStreamRunFailure(
    PersistRunFailure[ReactDepsT],
    Generic[ReactDepsT],
):
    async def wrap_run_event_stream(
        self,
        ctx: RunContext[ReactDepsT],
        *,
        stream: AsyncIterable[AgentStreamEvent],
    ) -> AsyncIterable[AgentStreamEvent]:
        try:
            async for event in stream:
                yield event
        except Exception:
            await self.record_failure(ctx)
            raise


@dataclass
class ReactDeps(Generic[ReactOutputT, ReactDepsT]):
    agent: Agent[ReactDepsT, ReactOutputT]
    conversation_manager: ConversationManager
    agent_deps: ReactDepsT
    thread_manager: ThreadManager | None = None
    event_send_stream: ObjectSendStream[ReactStreamEvent[ReactOutputT]] | None = None
    # Asked at each deferral, not held as one resolver for the run — see
    # `ResolverChoice`. The suspender is the other half: what an empty choice means.
    choose_resolvers: ResolverChoice | None = None
    suspender: DeferredSuspender | None = None
    output_type: OutputSpec[ReactOutputT] | None = None
    run_name: str = "react"
    # The directory this run happens in, recorded on every run it persists. None when
    # the run is in no project, since a react run has no directory of its own.
    cwd: DirectoryPath | None = None
    model: Model | KnownModelName | str | None = None
    instructions: AgentInstructions[ReactDepsT] = None
    model_settings: AgentModelSettings[ReactDepsT] | None = None
    usage_limits: UsageLimits | None = None
    usage: RunUsage | None = None
    metadata: AgentMetadata[ReactDepsT] | None = None
    output_retries: int | None = None
    infer_name: bool = True
    toolsets: Sequence[AbstractToolset[ReactDepsT]] | None = None
    builtin_tools: Sequence[AgentNativeTool[ReactDepsT]] | None = None
    capabilities: Sequence[AgentCapability[ReactDepsT]] | None = None
    spec: dict[str, Any] | AgentSpec | None = None


async def resolve_conversation(
    ctx: GraphRunContext[ReactState, ReactDeps[ReactOutputT, ReactDepsT]],
) -> Conversation:
    """The node's conversation: the pre-ensured one when the run is addressed
    by id, otherwise the agent's own (thread, agent) conversation."""
    if ctx.state.conversation_id is not None:
        conversation = await ctx.deps.conversation_manager.get(
            ctx.state.conversation_id
        )
        if (
            conversation.agent_tentacle_id != ctx.state.agent_tentacle_id
            or conversation.thread_id != ctx.state.thread_id
        ):
            raise ValueError(
                f"conversation {ctx.state.conversation_id} does not belong to "
                f"({ctx.state.agent_tentacle_id!r}, {ctx.state.thread_id})"
            )
        return conversation
    return await ctx.deps.conversation_manager.ensure(
        ctx.state.thread_id,
        agent_tentacle_id=ctx.state.agent_tentacle_id,
    )


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
            conversation = await resolve_conversation(ctx)
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
        with react_logfire.span(
            "react.agent_run",
            run_name=ctx.deps.run_name,
            streaming=ctx.deps.event_send_stream is not None,
            conversation_address=str(ctx.state.conversation_address),
            resumed=self.deferred_results is not None,
        ) as span:
            conversation = await resolve_conversation(ctx)
            persistence = RunPersistence(
                conversation_manager=ctx.deps.conversation_manager,
                thread_manager=ctx.deps.thread_manager,
                conversation=conversation,
                state=ctx.state,
                run_name=ctx.deps.run_name,
                cwd=ctx.deps.cwd,
                binds_prompt_sources=self.deferred_results is None,
            )
            capabilities = [
                (
                    PersistRunFailure(
                        persistence=persistence,
                        previous_message_count=len(conversation.messages),
                    )
                    if ctx.deps.event_send_stream is None
                    else PersistStreamRunFailure(
                        persistence=persistence,
                        previous_message_count=len(conversation.messages),
                    )
                ),
                *(ctx.deps.capabilities or []),
            ]
            if ctx.deps.event_send_stream is None:
                # builtin_tools and output_retries are deprecated run kwargs in
                # pydantic-ai 1.x: native tools register as NativeTool capabilities,
                # and the output-retry budget moves under retries={"output": ...}.
                # (The stream_events path below still takes the deprecated-shaped
                # kwargs; Agent.stream_events translates them internally.)
                retries = (
                    AgentRetries(output=ctx.deps.output_retries)
                    if ctx.deps.output_retries is not None
                    else None
                )
                if ctx.deps.builtin_tools:
                    capabilities = [
                        *capabilities,
                        *(NativeTool(tool) for tool in ctx.deps.builtin_tools),
                    ]
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
                    retries=retries,
                    infer_name=ctx.deps.infer_name,
                    toolsets=ctx.deps.toolsets,
                    capabilities=capabilities,
                    spec=ctx.deps.spec,
                )
                return await self.next_node(ctx, result, persistence, span)

            result: AgentRunResult[ReactOutputT] | None = None
            # stream_events (the normalizer) instead of run_stream_events: thinking +
            # tool/text events pass through raw, while structured segment replies
            # surface as ResultSegmentEvent for the consumer to render.
            async for event in ctx.deps.agent.stream_events(
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
                capabilities=capabilities,
                spec=ctx.deps.spec,
            ):
                if ctx.deps.event_send_stream is not None:
                    await ctx.deps.event_send_stream.send(event)
                if isinstance(event, AgentRunResultEvent):
                    result = event.result

            if result is None:
                raise RuntimeError(
                    "agent.stream_events did not yield AgentRunResultEvent"
                )
            return await self.next_node(ctx, result, persistence, span)

    async def next_node(
        self,
        ctx: GraphRunContext[ReactState, ReactDeps[ReactOutputT, ReactDepsT]],
        result: AgentRunResult[ReactOutputT],
        persistence: RunPersistence,
        span: logfire.LogfireSpan,
    ) -> ResolveDeferred[ReactOutputT, ReactDepsT] | End[AgentRunResult[ReactOutputT]]:
        new_messages = result.new_messages()
        span.set_attribute("react.run_id", result.run_id)
        span.set_attribute(
            "react.deferred", isinstance(result.output, DeferredToolRequests)
        )
        span.set_attribute("react.new_messages", len(new_messages))
        if new_messages:
            # Recording keeps the cached conversation coherent, so the next
            # RunAgent's ensure() picks up this turn from the manager — no copy in
            # state. Only the prompt turn binds source messages; deferred resumes
            # carry no new user request.
            await persistence.record(result.run_id, new_messages)

        if isinstance(result.output, DeferredToolRequests) and (
            ctx.deps.choose_resolvers is not None or ctx.deps.suspender is not None
        ):
            react_logfire.info("react run deferred", run_id=result.run_id)
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
        if ctx.deps.choose_resolvers is None and ctx.deps.suspender is None:
            raise RuntimeError(
                "ResolveDeferred requires a react graph resolver or suspender"
            )
        if ctx.deps.choose_resolvers is not None:
            # Asked here, at the deferral, rather than carried from the start of the
            # run: the conversation decides who answers this, and it can have been
            # told something new since the last time round.
            resolvers = ctx.deps.choose_resolvers(await resolve_conversation(ctx))
            results = DeferredToolResults()
            for resolver in resolvers:
                answered = await resolver.resolve(self.requests)
                # Earlier resolvers win — each answers what it speaks for and a later
                # one only fills what is still open, which is what puts a catch-all
                # last. Each sees the batch entire rather than the remainder, so one
                # that classifies by `metadata` keeps what it classifies by.
                for tool_call_id, value in answered.calls.items():
                    results.calls.setdefault(tool_call_id, value)
                for tool_call_id, verdict in answered.approvals.items():
                    results.approvals.setdefault(tool_call_id, verdict)
            # All of the batch or none of it: the runner refuses results covering only
            # some of what it deferred, so a batch nobody wholly answered is the
            # human's — along with the parts of it that were answerable.
            covered = all(
                call.tool_call_id in results.calls for call in self.requests.calls
            ) and all(
                approval.tool_call_id in results.approvals
                for approval in self.requests.approvals
            )
            if resolvers and covered:
                react_logfire.info(
                    "deferred resolved in-process, looping back to RunAgent"
                )
                return RunAgent(deferred_results=results)
        if ctx.deps.suspender is not None:
            react_logfire.info("deferred suspended, ending run")
            event = await ctx.deps.suspender.suspend(self.requests)
            if event is not None and ctx.deps.event_send_stream is not None:
                await ctx.deps.event_send_stream.send(event)
            return End(self.result)
        # Nothing in-process took it and there is no human to ask, so the requests go
        # back to the caller — the same end a graph carrying neither hook reaches.
        return End(self.result)


ReactGraphInput = TypeAliasType(
    "ReactGraphInput",
    StartTurn[ReactOutputT, ReactDepsT] | ResumeTurn[ReactOutputT, ReactDepsT],
    type_params=(ReactOutputT, ReactDepsT),
)


def build_react_graph(
    start_node: ReactGraphInput[ReactOutputT, ReactDepsT],
) -> Graph[
    ReactState,
    ReactDeps[ReactOutputT, ReactDepsT],
    ReactGraphInput[ReactOutputT, ReactDepsT],
    AgentRunResult[ReactOutputT],
]:
    """Wire the react nodes into a runnable graph for `start_node` to run through.

    The node it will be run with is what binds the graph's type parameters — the
    output and deps types live only in annotations, so there is nothing else here
    to infer them from.
    """
    builder = GraphBuilder(
        name="react",
        state_type=ReactState,
        deps_type=ReactDeps[ReactOutputT, ReactDepsT],
        input_type=TypeExpression[ReactGraphInput[ReactOutputT, ReactDepsT]],
        output_type=AgentRunResult[ReactOutputT],
    )
    # Every other edge comes from the nodes' own `run` return annotations, so a
    # transition stays declared where it is written. The one thing those cannot
    # express is where a run *starts* — a turn either begins fresh or resumes
    # deferred results — so the entry is a decision on the input's own type.
    entry = (
        builder.decision(note="a fresh turn, or one resuming deferred results")
        .branch(builder.match(StartTurn).to(StartTurn))
        .branch(builder.match(ResumeTurn).to(ResumeTurn))
    )
    builder.add(
        builder.edge_from(builder.start_node).to(entry),
        builder.node(StartTurn[ReactOutputT, ReactDepsT]),
        builder.node(ResumeTurn[ReactOutputT, ReactDepsT]),
        builder.node(RunAgent[ReactOutputT, ReactDepsT]),
        builder.node(ResolveDeferred[ReactOutputT, ReactDepsT]),
    )
    return builder.build()


async def iter_react_graph_events(
    start_node: StartTurn[ReactOutputT, ReactDepsT]
    | ResumeTurn[ReactOutputT, ReactDepsT],
    *,
    state: ReactState,
    deps: ReactDeps[ReactOutputT, ReactDepsT],
) -> AsyncGenerator[ReactStreamEvent[ReactOutputT], None]:
    send_stream, receive_stream = anyio.create_memory_object_stream[
        ReactStreamEvent[ReactOutputT]
    ](100)
    graph_deps = replace(deps, event_send_stream=send_stream)
    captured: list[Exception] = []

    async def run_graph() -> None:
        # Capture the graph error instead of letting it escape: a raising child
        # task trips the task group's cancel scope and cancels the consumer
        # mid-event (e.g. blocked on a channel send), which masks the real error
        # and leaves spans unclosed. Closing send_stream ends the consumer loop
        # cleanly; we re-raise below, in the consumer's own frame.
        graph = build_react_graph(start_node)
        try:
            async with send_stream:
                await graph.run(
                    inputs=start_node,
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


class ReactEventStream(Generic[ReactOutputCoT]):
    """Deterministic-cleanup handle over a react event stream: entering the
    context yields the underlying generator, exiting closes it. Mirrors
    pydantic-ai's ``AgentEventStream``, but typed over ``ReactStreamEvent`` — a
    react run also streams octomate output/display/action-batch events."""

    # Private so the covariant type parameter has no mutable public surface.
    def __init__(
        self, generator: AsyncGenerator[ReactStreamEvent[ReactOutputCoT], None]
    ) -> None:
        self._generator = generator

    async def __aenter__(
        self,
    ) -> AsyncGenerator[ReactStreamEvent[ReactOutputCoT], None]:
        return self._generator

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._generator.aclose()
