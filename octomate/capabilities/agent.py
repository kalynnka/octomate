"""`Agent`: a Pydantic AI `Agent` subclass that streams events AND the typed output.

`run_stream_events` gives raw events but not validated output; `stream_output`
gives validated partial output but not thinking/tool events. `stream_events` drives
`iter()` ourselves and emits BOTH from each node:

- thinking + tool-call events pass through unchanged,
- the reply is emitted as `OutputDeltaEvent[OutputT]` (partial, validated with
  `allow_partial=True`) then Pydantic AI's own `FinalResult[OutputT]` as the final
  value, generic over the run's output type,
- a terminal `AgentRunResultEvent` closes the stream (run-complete / deferred).

The two `output_type` overloads carry the output type into the event stream, so
`Agent[Deps, list[Row]].stream_events(...)` yields `OutputDeltaEvent[list[Row]]`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, overload

from pydantic import ValidationError
from pydantic_ai import Agent as PydanticAgent
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.agent.abstract import RunOutputDataT
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import FinalResultEvent, ModelMessage, UserContent
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.output import OutputDataT, OutputSpec
from pydantic_ai.result import FinalResult
from pydantic_ai.tools import AgentDepsT, DeferredToolResults

from octomate.capabilities.events import OutputDeltaEvent, StreamEvents

NO_OUTPUT = object()


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
        deps: AgentDepsT = None,
        model: Model | KnownModelName | str | None = None,
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
        deps: AgentDepsT = None,
        model: Model | KnownModelName | str | None = None,
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
        deps: AgentDepsT = None,
        model: Model | KnownModelName | str | None = None,
    ) -> AsyncIterator[StreamEvents[Any] | AgentRunResultEvent[Any]]:
        async with self.iter(
            user_prompt,
            output_type=output_type,
            message_history=message_history,
            deferred_tool_results=deferred_tool_results,
            deps=deps,
            model=model,
        ) as run:
            async for node in run:
                if self.is_model_request_node(node):
                    # The model node streams the reply: passthrough thinking/pre-output
                    # events, then map the output (after FinalResultEvent) to Output events.
                    async with node.stream(run.ctx) as stream:
                        final_event: FinalResultEvent | None = None
                        last: object = NO_OUTPUT
                        async for event in stream:
                            if isinstance(event, FinalResultEvent):
                                final_event = event
                                continue
                            if final_event is None:
                                yield event
                                continue
                            try:
                                partial = await stream.validate_response_output(
                                    stream.response, allow_partial=True
                                )
                            except (ValidationError, ModelRetry):
                                continue
                            if last is NO_OUTPUT or partial != last:
                                last = partial
                                yield OutputDeltaEvent(output=partial)
                        if final_event is not None:
                            try:
                                final = await stream.validate_response_output(
                                    stream.response
                                )
                            except (ValidationError, ModelRetry):
                                final = None
                            yield FinalResult(
                                output=final,
                                tool_name=final_event.tool_name,
                                tool_call_id=final_event.tool_call_id,
                            )
                elif self.is_call_tools_node(node):
                    # The tools node streams tool call/result events — pass them through.
                    async with node.stream(run.ctx) as tool_stream:
                        async for tool_event in tool_stream:
                            yield tool_event
            if run.result is not None:
                yield AgentRunResultEvent(run.result)
