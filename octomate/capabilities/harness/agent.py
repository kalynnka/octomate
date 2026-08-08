"""`Agent`: a Pydantic AI `Agent` subclass that streams events AND the typed output.

`run_stream_events` gives raw events but not validated output; `stream_output`
gives validated partial output but not thinking/tool events. `stream_events` drives
`iter()` ourselves and emits BOTH from each node:

- thinking + tool-call events pass through unchanged,
- text replies stream as Pydantic AI's native text events; every validated output
  type surfaces as Pydantic AI's own `FinalResult[OutputT]`; outputs that
  validate to `list[MessageSegment]` also stream one `ResultSegmentEvent` per
  segment as partial validation reveals them. Other structured outputs surface
  only at `FinalResult`.
- capability-injected events (e.g. todo events) flow through: each node's stream is
  wrapped with the run's capabilities' `wrap_run_event_stream`, which the manual
  `iter()` path would otherwise bypass (pydantic-ai applies it only inside `run()`),
- a terminal `AgentRunResultEvent` closes the stream (run-complete / deferred).

The two `output_type` overloads carry the output type into the event stream, so the
final `Agent[Deps, list[Row]].stream_events(...)` value is a `FinalResult[list[Row]]`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, cast, overload

from pydantic import ValidationError
from pydantic_ai import Agent as PydanticAgent
from pydantic_ai import (
    AgentCapability,
    AgentModelSettings,
    AgentNativeTool,
    AgentRunResultEvent,
    AgentSpec,
    RunUsage,
    UsageLimits,
)

# Pydantic AI applies capability wrap_run_event_stream only inside run()'s hooked
# path; stream_events drives iter() directly, so we replicate the wrap per node.
# build_run_context is private — no public RunContext-from-AgentRun exists in 1.93.0.
from pydantic_ai._agent_graph import build_run_context
from pydantic_ai.agent.abstract import (
    AgentInstructions,
    AgentMetadata,
    AgentRetries,
    RunOutputDataT,
)
from pydantic_ai.capabilities import NativeTool
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import (
    FinalResultEvent,
    ModelMessage,
    UserContent,
)
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.output import OutputDataT, OutputSpec
from pydantic_ai.result import FinalResult
from pydantic_ai.tools import AgentDepsT, DeferredToolResults
from pydantic_ai.toolsets import AbstractToolset
from pydantic_graph import End

from octomate.capabilities.harness.events import (
    ResultSegmentEvent,
    ResultTextDeltaEvent,
    StreamEvents,
)
from octomate.schemas.segments import (
    MarkdownSegment,
    MessageSegment,
    Segment,
    TextSegment,
)


class Agent(PydanticAgent[AgentDepsT, OutputDataT]):
    """A Pydantic AI agent that can stream events and the typed output together."""

    @overload
    def stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        output_type: None = None,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        conversation_id: str | None = None,
        deps: AgentDepsT = None,
        model: Model | KnownModelName | str | None = None,
        instructions: AgentInstructions[AgentDepsT] = None,
        model_settings: AgentModelSettings[AgentDepsT] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[AgentDepsT] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[AgentDepsT]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[AgentDepsT]] | None = None,
        capabilities: Sequence[AgentCapability[AgentDepsT]] | None = None,
        spec: dict[str, Any] | AgentSpec | None = None,
    ) -> AsyncIterator[
        StreamEvents[OutputDataT] | AgentRunResultEvent[OutputDataT]
    ]: ...

    @overload
    def stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        output_type: OutputSpec[RunOutputDataT],
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        conversation_id: str | None = None,
        deps: AgentDepsT = None,
        model: Model | KnownModelName | str | None = None,
        instructions: AgentInstructions[AgentDepsT] = None,
        model_settings: AgentModelSettings[AgentDepsT] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[AgentDepsT] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[AgentDepsT]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[AgentDepsT]] | None = None,
        capabilities: Sequence[AgentCapability[AgentDepsT]] | None = None,
        spec: dict[str, Any] | AgentSpec | None = None,
    ) -> AsyncIterator[
        StreamEvents[RunOutputDataT] | AgentRunResultEvent[RunOutputDataT]
    ]: ...

    async def stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        output_type: OutputSpec[Any] | None = None,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        conversation_id: str | None = None,
        deps: AgentDepsT = None,
        model: Model | KnownModelName | str | None = None,
        instructions: AgentInstructions[AgentDepsT] = None,
        model_settings: AgentModelSettings[AgentDepsT] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[AgentDepsT] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[AgentDepsT]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[AgentDepsT]] | None = None,
        capabilities: Sequence[AgentCapability[AgentDepsT]] | None = None,
        spec: dict[str, Any] | AgentSpec | None = None,
    ) -> AsyncIterator[StreamEvents[Any] | AgentRunResultEvent[Any]]:
        # builtin_tools and output_retries are deprecated run kwargs in pydantic-ai
        # 1.x: native tools register as NativeTool capabilities, and the output-retry
        # budget moves under retries={"output": ...}.
        retries = (
            AgentRetries(output=output_retries) if output_retries is not None else None
        )
        if builtin_tools:
            capabilities = [
                *(capabilities or []),
                *(NativeTool(tool) for tool in builtin_tools),
            ]
        async with self.iter(
            user_prompt,
            output_type=output_type,
            message_history=message_history,
            deferred_tool_results=deferred_tool_results,
            conversation_id=conversation_id,
            deps=deps,
            model=model,
            instructions=instructions,
            model_settings=model_settings,
            usage_limits=usage_limits,
            usage=usage,
            metadata=metadata,
            retries=retries,
            infer_name=infer_name,
            toolsets=toolsets,
            capabilities=capabilities,
            spec=spec,
        ) as run:
            # Driven by hand rather than `async for node in run`: that path is the
            # graph's own iteration, which `AgentRun.__anext__` documents as calling
            # none of the node hooks, so a capability built on them would be silently
            # inert here while working on a plain agent. The order below is the one
            # `run_stream_events` uses for the same reason we need it — `before_node_run`
            # may replace the node, so it has to fire before the node streams, and the
            # rest of the lifecycle can only run once the stream is drained.
            node = run.next_node
            while not isinstance(node, End):
                # A `wrap_run` hook can answer without the graph running at all.
                if run.result is not None:
                    break
                capability = run.ctx.deps.root_capability
                node = await capability.before_node_run(
                    build_run_context(run.ctx), node=node
                )
                if self.is_model_request_node(node):
                    # The model node streams thinking/pre-output events unchanged.
                    # Plain-text output (FinalResultEvent.tool_name is None) keeps
                    # streaming its native text events after the final result is
                    # recognized — that text *is* the answer. Tool-backed structured
                    # outputs instead surface segment-list elements as
                    # ResultSegmentEvent; every output type still reaches FinalResult.
                    async with node.stream(run.ctx) as stream:
                        wrapped = capability.wrap_run_event_stream(
                            build_run_context(run.ctx), stream=stream
                        )
                        final_event: FinalResultEvent | None = None
                        emitted_segments = 0
                        # The element whose text is streaming as deltas, and how much
                        # of it has already been emitted.
                        streamed_index: int | None = None
                        streamed_chars = 0

                        def settle_element(
                            segment: object,
                            index: int,
                            streamed_index: int | None,
                            streamed_chars: int,
                        ) -> ResultSegmentEvent | ResultTextDeltaEvent | None:
                            # A validated segment-list element is a union member; the
                            # Annotated discriminated union can't be
                            # isinstance-narrowed, so guard on the base then cast.
                            if not isinstance(segment, Segment):
                                return None
                            if index == streamed_index:
                                # Already rendered as deltas; only the text revealed
                                # since the last one is still owed.
                                remainder = str(segment)[streamed_chars:]
                                if not remainder:
                                    return None
                                return ResultTextDeltaEvent(delta=remainder)
                            return ResultSegmentEvent(
                                segment=cast(MessageSegment, segment)
                            )

                        # A capability injecting a non-AgentStreamEvent before the
                        # FinalResultEvent passes through here; injecting after it is
                        # not supported (todo events inject on the tools node).
                        async for event in wrapped:
                            if isinstance(event, FinalResultEvent):
                                final_event = event
                                continue
                            # Pre-final events pass through; once a plain-text final
                            # result is recognized its native text deltas keep flowing.
                            if final_event is None or final_event.tool_name is None:
                                yield event
                                continue
                            # After a tool-backed final result is recognized, outputs
                            # that validate into a growing list of message segments are
                            # surfaced one segment at a time. Other structured
                            # outputs are intentionally left for FinalResult.
                            try:
                                partial = await stream.validate_response_output(
                                    stream.response, allow_partial=True
                                )
                            except (ValidationError, ModelRetry):
                                continue
                            # TODO: emit all the items from a iterable structured output
                            # as they validate, not only lists of segments.
                            if isinstance(partial, list):
                                # Elements a later one has sealed are final — emit them
                                # whole, except the one already streaming as deltas,
                                # which settles with its remainder.
                                for index in range(emitted_segments, len(partial) - 1):
                                    settled = settle_element(
                                        partial[index],
                                        index,
                                        streamed_index,
                                        streamed_chars,
                                    )
                                    if settled is not None:
                                        yield settled
                                emitted_segments = max(
                                    emitted_segments, len(partial) - 1
                                )
                                # The trailing element may still be growing under
                                # partial validation. A text-bearing tail streams its
                                # growth immediately — the visible reply must not wait
                                # for the model to finish; other shapes wait to be
                                # sealed (or for the final validated output below).
                                tail_index = len(partial) - 1
                                if tail_index >= emitted_segments and isinstance(
                                    partial[tail_index], TextSegment | MarkdownSegment
                                ):
                                    if streamed_index != tail_index:
                                        streamed_index = tail_index
                                        streamed_chars = 0
                                    text = str(partial[tail_index])
                                    if len(text) > streamed_chars:
                                        delta = text[streamed_chars:]
                                        streamed_chars = len(text)
                                        yield ResultTextDeltaEvent(delta=delta)
                        if final_event is not None:
                            try:
                                final = await stream.validate_response_output(
                                    stream.response
                                )
                            except (ValidationError, ModelRetry):
                                final = None
                            if isinstance(final, list):
                                for index in range(emitted_segments, len(final)):
                                    settled = settle_element(
                                        final[index],
                                        index,
                                        streamed_index,
                                        streamed_chars,
                                    )
                                    if settled is not None:
                                        yield settled
                            yield FinalResult(
                                output=final,
                                tool_name=final_event.tool_name,
                                tool_call_id=final_event.tool_call_id,
                            )
                elif self.is_call_tools_node(node):
                    # The tools node streams tool call/result events. Wrapping with the
                    # run's capabilities lets capability-injected events (e.g. todo
                    # events stashed on a ToolReturn) reach the consumer.
                    async with node.stream(run.ctx) as tool_stream:
                        wrapped = capability.wrap_run_event_stream(
                            build_run_context(run.ctx), stream=tool_stream
                        )
                        async for tool_event in wrapped:
                            yield tool_event
                # `wrap_node_run` → `on_node_run_error` → `after_node_run`, around the
                # step that advances the graph. The context is rebuilt so those hooks
                # see the state the streaming above left behind (`run_step`), matching
                # what `run_stream_events` does at the same point.
                node = await run._wrap_and_advance(  # pyright: ignore[reportPrivateUsage]
                    build_run_context(run.ctx),
                    node,
                    run._advance_graph,  # pyright: ignore[reportPrivateUsage]
                )
            if run.result is not None:
                yield AgentRunResultEvent(run.result)
