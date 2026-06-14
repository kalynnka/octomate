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
    AgentBuiltinTool,
    AgentCapability,
    AgentModelSettings,
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
    RunOutputDataT,
)
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

from octomate.capabilities.events import (
    ResultSegmentEvent,
    StreamEvents,
)
from octomate.schemas.segments import OutputSegment, Segment


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
        builtin_tools: Sequence[AgentBuiltinTool[AgentDepsT]] | None = None,
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
        builtin_tools: Sequence[AgentBuiltinTool[AgentDepsT]] | None = None,
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
        builtin_tools: Sequence[AgentBuiltinTool[AgentDepsT]] | None = None,
        capabilities: Sequence[AgentCapability[AgentDepsT]] | None = None,
        spec: dict[str, Any] | AgentSpec | None = None,
    ) -> AsyncIterator[StreamEvents[Any] | AgentRunResultEvent[Any]]:
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
            output_retries=output_retries,
            infer_name=infer_name,
            toolsets=toolsets,
            builtin_tools=builtin_tools,
            capabilities=capabilities,
            spec=spec,
        ) as run:
            async for node in run:
                if self.is_model_request_node(node):
                    # The model node streams thinking/pre-output events unchanged.
                    # Plain-text output (FinalResultEvent.tool_name is None) keeps
                    # streaming its native text events after the final result is
                    # recognized — that text *is* the answer. Tool-backed structured
                    # outputs instead surface segment-list elements as
                    # ResultSegmentEvent; every output type still reaches FinalResult.
                    async with node.stream(run.ctx) as stream:
                        wrapped = run.ctx.deps.root_capability.wrap_run_event_stream(
                            build_run_context(run.ctx), stream=stream
                        )
                        final_event: FinalResultEvent | None = None
                        emitted_segments = 0
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
                                # The trailing element may still be growing under
                                # partial validation — emit only the elements a later
                                # one has sealed; the tail is emitted from the final
                                # validated output below.
                                for segment in partial[emitted_segments:-1]:
                                    # A validated segment-list element is a union member;
                                    # the Annotated discriminated union can't be
                                    # isinstance-narrowed, so guard on the base then cast.
                                    if isinstance(segment, Segment):
                                        yield ResultSegmentEvent(
                                            segment=cast(OutputSegment, segment)
                                        )
                                emitted_segments = max(
                                    emitted_segments, len(partial) - 1
                                )
                        if final_event is not None:
                            try:
                                final = await stream.validate_response_output(
                                    stream.response
                                )
                            except (ValidationError, ModelRetry):
                                final = None
                            if isinstance(final, list):
                                for segment in final[emitted_segments:]:
                                    if isinstance(segment, Segment):
                                        yield ResultSegmentEvent(
                                            segment=cast(OutputSegment, segment)
                                        )
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
                        wrapped = run.ctx.deps.root_capability.wrap_run_event_stream(
                            build_run_context(run.ctx), stream=tool_stream
                        )
                        async for tool_event in wrapped:
                            yield tool_event
            if run.result is not None:
                yield AgentRunResultEvent(run.result)
