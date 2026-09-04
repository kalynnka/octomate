from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from pydantic import TypeAdapter, ValidationError
from pydantic_ai import AgentRunResult

# GraphAgentState is private pydantic-ai internals. A dsh run produces no real
# pydantic-ai graph state, so we synthesize the minimal state an AgentRunResult
# needs (message history + run/conversation id), exactly as the Codex adapter does.
from pydantic_ai._agent_graph import GraphAgentState
from pydantic_ai.messages import (
    FinishReason,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.messages import ModelRequest as PydanticModelRequest
from pydantic_ai.usage import RequestUsage, RunUsage

from octomate.capabilities.harness.events import StreamEvents
from octomate.tentacles.deepseek.wire import (
    DeepseekUsage,
    ModelRoute,
    ReasoningDeltaChunk,
    SessionEvent,
    SessionEventFrame,
    TextDeltaChunk,
    ToolCallDeltaChunk,
    UsageChunk,
    assistant_message_of,
    chunk_delta,
    provenance_of,
    request_route_of,
    text_of,
    tool_call_of,
    tool_result_of,
    turn_end_of,
)
from octomate.types.json import JsonObject, JsonValue

ToolOutcome: TypeAlias = Literal["success", "failed"]

DEEPSEEK_PROVIDER_NAME = "deepseek"
DEEPSEEK_METADATA_SOURCE = "deepseek"

# dsh's turn-end reason kinds onto pydantic-ai finish reasons. The reason map is
# merge-extensible upstream, so an unknown kind maps to no finish reason at all.
DEEPSEEK_FINISH_REASON_MAP: dict[str, FinishReason] = {
    "completed": "stop",
    "aborted": "stop",
    "interrupted": "stop",
    "error": "error",
    "max-tokens": "length",
}

json_object_adapter = TypeAdapter(JsonObject)


@dataclass
class StreamingPartState:
    index: int
    part: TextPart | ThinkingPart
    events: list[JsonValue] = field(default_factory=list)


@dataclass
class NativeToolState:
    live_call: ToolCallPart
    native_call: NativeToolCallPart
    # The raw argument JSON accumulated from tool-call deltas; the `tool/call`
    # event's full string is authoritative when it arrives.
    arguments: str = ""
    events: list[JsonValue] = field(default_factory=list)


def map_usage(usage: DeepseekUsage) -> RequestUsage:
    details = (
        {"reasoning_tokens": usage.reasoning_tokens}
        if usage.reasoning_tokens is not None
        else {}
    )
    return RequestUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens or 0,
        cache_write_tokens=usage.cache_write_tokens or 0,
        details=details,
    )


def frame_event(frame: SessionEventFrame) -> JsonObject:
    """One mux frame as replay metadata: the session event verbatim, plus the
    host-computed render `view` when the frame carried one."""
    payload = json_object_adapter.validate_python(
        frame.event.model_dump(mode="json", by_alias=True)
    )
    if frame.view is not None:
        payload["view"] = frame.view
    return payload


def deepseek_metadata(events: list[JsonValue] | None = None) -> JsonObject:
    metadata: JsonObject = {"source": DEEPSEEK_METADATA_SOURCE}
    if events:
        metadata["events"] = events
    return metadata


def parse_arguments(raw: str) -> JsonObject | str:
    """The raw argument JSON the model produced: parsed when it is an object,
    kept raw otherwise — dsh does not parse it either."""
    if not raw:
        return {}
    try:
        return json_object_adapter.validate_json(raw)
    except ValidationError:
        return raw


class DeepseekRunAccumulator:
    """Translate a dsh session-event stream into Octomate run projections.

    Consumes the `session/event` mux frames of one driven turn — gated from
    `turn/start` (so the tail of an earlier turn cannot bleed in) to
    `turn/end` — and folds them into pydantic-ai stream events plus the
    persisted `ModelMessage` history, mirroring `CodexRunAccumulator`.
    """

    def __init__(self) -> None:
        self.messages: list[ModelMessage] = []
        self.usage = RunUsage()
        self.result_text = ""
        self.turn_started = False
        self.turn_ended = False
        self.turn_error: str | None = None
        self.finish_reason: FinishReason | None = None
        self.route: ModelRoute | None = None
        self.part_index = 0
        # Parts of the in-flight assistant response: `current` is receiving
        # deltas, `closed_parts` have ended and await the message commit. dsh
        # chunks carry no item id, so a part closes on kind switch or commit.
        self.current: StreamingPartState | None = None
        self.closed_parts: list[StreamingPartState] = []
        self.native_tools: dict[str, NativeToolState] = {}
        self.pending_events: list[JsonValue] = []
        self.step_usage: RequestUsage | None = None

    def begin(self, user_prompt: str | Sequence[UserContent] | None) -> None:
        if user_prompt:
            self.messages.append(
                PydanticModelRequest(
                    parts=[UserPromptPart(content=user_prompt)],
                    metadata=deepseek_metadata(),
                )
            )

    def consume(self, frame: SessionEventFrame) -> Iterator[StreamEvents[str]]:
        event = frame.event
        payload = frame_event(frame)
        if not self.turn_started:
            if event.type == "turn/start":
                self.turn_started = True
                self.pending_events.append(payload)
            # Anything earlier is the tail of a turn that is not this run's:
            # not rendered, and not this run's metadata either.
            return
        if self.turn_ended:
            return
        if event.type == "assistant/chunk":
            yield from self.consume_chunk(event, payload)
        elif event.type == "assistant/message":
            yield from self.consume_message(event, payload)
        elif event.type == "tool/call":
            yield from self.consume_tool_call(event, payload)
        elif event.type == "tool/result":
            yield from self.consume_tool_result(event, payload)
        elif event.type == "request/context":
            route = request_route_of(event)
            if route is not None:
                self.route = route
            self.pending_events.append(payload)
        elif event.type == "turn/end":
            yield from self.consume_turn_end(event, payload)
        else:
            self.pending_events.append(payload)

    def build_result(self, run_id: str, conversation_id: str) -> AgentRunResult[str]:
        state = GraphAgentState(
            message_history=self.messages,
            usage=self.usage,
            run_id=run_id,
            conversation_id=conversation_id,
        )
        return AgentRunResult(output=self.result_text, _state=state)

    def take_part_index(self) -> int:
        index = self.part_index
        self.part_index += 1
        return index

    def consume_chunk(
        self, event: SessionEvent, payload: JsonObject
    ) -> Iterator[StreamEvents[str]]:
        delta = chunk_delta(event)
        if isinstance(delta, TextDeltaChunk):
            yield from self.stream_delta(TextPart, delta.text, payload)
        elif isinstance(delta, ReasoningDeltaChunk):
            yield from self.stream_delta(ThinkingPart, delta.text, payload)
        elif isinstance(delta, ToolCallDeltaChunk):
            yield from self.stream_tool_arguments(delta, payload)
        elif isinstance(delta, UsageChunk):
            self.step_usage = map_usage(delta.usage)
            self.pending_events.append(payload)
        else:
            self.pending_events.append(payload)

    def stream_delta(
        self,
        kind: type[TextPart] | type[ThinkingPart],
        text: str,
        payload: JsonObject,
    ) -> Iterator[StreamEvents[str]]:
        current = self.current
        if current is None or not isinstance(current.part, kind):
            if current is not None:
                yield PartEndEvent(index=current.index, part=current.part)
                self.closed_parts.append(current)
            part = kind(content="", provider_name=DEEPSEEK_PROVIDER_NAME)
            current = StreamingPartState(index=self.take_part_index(), part=part)
            self.current = current
            yield PartStartEvent(index=current.index, part=part)
        current.events.append(payload)
        current.part.content += text
        if isinstance(current.part, TextPart):
            yield PartDeltaEvent(
                index=current.index,
                delta=TextPartDelta(text, provider_name=DEEPSEEK_PROVIDER_NAME),
            )
        else:
            yield PartDeltaEvent(
                index=current.index,
                delta=ThinkingPartDelta(
                    content_delta=text, provider_name=DEEPSEEK_PROVIDER_NAME
                ),
            )

    def open_native_tool(
        self, call_id: str, name: str, payload: JsonObject
    ) -> NativeToolState:
        live_call = ToolCallPart(
            tool_name=name,
            args="",
            tool_call_id=call_id,
            provider_name=DEEPSEEK_PROVIDER_NAME,
        )
        native_call = NativeToolCallPart(
            tool_name=name,
            args="",
            tool_call_id=call_id,
            provider_name=DEEPSEEK_PROVIDER_NAME,
        )
        state = NativeToolState(
            live_call=live_call, native_call=native_call, events=[payload]
        )
        self.native_tools[call_id] = state
        return state

    def stream_tool_arguments(
        self, delta: ToolCallDeltaChunk, payload: JsonObject
    ) -> Iterator[StreamEvents[str]]:
        state = self.native_tools.get(delta.id)
        if state is None:
            state = self.open_native_tool(delta.id, delta.name or "", payload)
            yield FunctionToolCallEvent(part=state.live_call)
        else:
            state.events.append(payload)
            if delta.name:
                state.live_call.tool_name = delta.name
                state.native_call.tool_name = delta.name
        state.arguments += delta.arguments_delta

    def consume_tool_call(
        self, event: SessionEvent, payload: JsonObject
    ) -> Iterator[StreamEvents[str]]:
        call = tool_call_of(event)
        if call is None:
            self.pending_events.append(payload)
            return
        state = self.native_tools.get(call.call_id)
        if state is None:
            state = self.open_native_tool(call.call_id, call.name, payload)
            yield FunctionToolCallEvent(part=state.live_call)
        else:
            state.events.append(payload)
        state.arguments = call.arguments or state.arguments
        args = parse_arguments(state.arguments)
        state.live_call.tool_name = call.name
        state.live_call.args = args
        state.native_call.tool_name = call.name
        state.native_call.args = args

    def consume_tool_result(
        self, event: SessionEvent, payload: JsonObject
    ) -> Iterator[StreamEvents[str]]:
        result = tool_result_of(event)
        if result is None:
            self.pending_events.append(payload)
            return
        state = self.native_tools.pop(result.call_id, None)
        if state is None:
            # A result whose call never streamed: synthesize the pairing so the
            # history stays whole. dsh's result event does not carry the tool
            # name, so the placeholder is all there is.
            state = self.open_native_tool(result.call_id, "deepseek_tool", payload)
            del self.native_tools[result.call_id]
            yield FunctionToolCallEvent(part=state.live_call)
        else:
            state.events.append(payload)
        if state.arguments and not state.native_call.args:
            args = parse_arguments(state.arguments)
            state.live_call.args = args
            state.native_call.args = args
        outcome: ToolOutcome = "failed" if result.is_error else "success"
        live_return = ToolReturnPart(
            tool_name=state.live_call.tool_name,
            content=result.text,
            tool_call_id=result.call_id,
            outcome=outcome,
        )
        native_return = NativeToolReturnPart(
            tool_name=state.live_call.tool_name,
            content=result.text,
            tool_call_id=result.call_id,
            provider_name=DEEPSEEK_PROVIDER_NAME,
            outcome=outcome,
        )
        yield FunctionToolResultEvent(part=live_return)
        self.messages.append(
            ModelResponse(
                parts=[state.native_call, native_return],
                provider_name=DEEPSEEK_PROVIDER_NAME,
                provider_response_id=result.call_id,
                metadata=deepseek_metadata(self.take_message_events(state.events)),
            )
        )

    def consume_message(
        self, event: SessionEvent, payload: JsonObject
    ) -> Iterator[StreamEvents[str]]:
        data = assistant_message_of(event)
        committed = text_of(data.message.content) if data is not None else ""
        provenance = provenance_of(event)
        if provenance is not None:
            self.route = provenance
        current = self.current
        if current is not None:
            if isinstance(current.part, TextPart) and committed:
                # The commit is authoritative over the streamed accumulation.
                current.part.content = committed
            yield PartEndEvent(index=current.index, part=current.part)
            self.closed_parts.append(current)
            self.current = None
        if committed and not any(
            isinstance(state.part, TextPart) for state in self.closed_parts
        ):
            part = TextPart(content=committed, provider_name=DEEPSEEK_PROVIDER_NAME)
            state = StreamingPartState(index=self.take_part_index(), part=part)
            yield PartStartEvent(index=state.index, part=part)
            yield PartEndEvent(index=state.index, part=part)
            self.closed_parts.append(state)
        if committed:
            # The turn's last assistant message wins, matching how dsh's own
            # clients read the stream.
            self.result_text = committed
        if not self.closed_parts:
            # A message with no text and nothing streamed (tool-call blocks
            # only): the tool pairing renders the activity, keep the raw event.
            self.pending_events.append(payload)
            return
        usage = (
            map_usage(data.usage)
            if data is not None and data.usage is not None
            else self.step_usage
        )
        self.commit_response(usage, payload)

    def commit_response(self, usage: RequestUsage | None, payload: JsonObject) -> None:
        part_events = [event for state in self.closed_parts for event in state.events]
        response = ModelResponse(
            parts=[state.part for state in self.closed_parts],
            provider_name=DEEPSEEK_PROVIDER_NAME,
            model_name=self.route.model if self.route is not None else None,
            usage=usage or RequestUsage(),
            metadata=deepseek_metadata(
                self.take_message_events([*part_events, payload])
            ),
        )
        if self.route is not None:
            response.provider_details = {
                "provider": self.route.provider,
                "model": self.route.model,
            }
        self.messages.append(response)
        if usage is not None:
            self.usage.incr(usage)
        self.usage.requests += 1
        self.closed_parts = []
        self.step_usage = None

    def consume_turn_end(
        self, event: SessionEvent, payload: JsonObject
    ) -> Iterator[StreamEvents[str]]:
        self.turn_ended = True
        end = turn_end_of(event)
        reason = end.reason if end is not None else None
        kind = reason.kind if reason is not None else "completed"
        self.finish_reason = DEEPSEEK_FINISH_REASON_MAP.get(kind)
        if kind == "error":
            message = reason.error_message if reason is not None else None
            self.turn_error = message or "dsh turn failed"
        current = self.current
        if current is not None:
            # An aborted turn has streamed parts no assistant/message will ever
            # commit; close and commit them so the partial reply persists.
            yield PartEndEvent(index=current.index, part=current.part)
            self.closed_parts.append(current)
            self.current = None
        if self.closed_parts:
            if not self.result_text:
                for state in reversed(self.closed_parts):
                    if isinstance(state.part, TextPart):
                        self.result_text = state.part.content
                        break
            self.commit_response(self.step_usage, payload)
        else:
            self.pending_events.append(payload)
        for message in reversed(self.messages):
            if isinstance(message, ModelResponse):
                message.finish_reason = self.finish_reason
                provider_details = dict(message.provider_details or {})
                provider_details["turn_end"] = kind
                if self.turn_error is not None:
                    provider_details["error"] = self.turn_error
                message.provider_details = provider_details
                break
        else:
            if self.turn_error is not None:
                self.messages.append(
                    ModelResponse(
                        parts=[
                            TextPart(
                                content=self.turn_error,
                                provider_name=DEEPSEEK_PROVIDER_NAME,
                            )
                        ],
                        provider_name=DEEPSEEK_PROVIDER_NAME,
                        finish_reason="error",
                        provider_details={"turn_end": kind, "error": self.turn_error},
                        metadata=deepseek_metadata(self.take_message_events([])),
                    )
                )

    def complete_command(self, text: str) -> Iterator[StreamEvents[str]]:
        """A slash-intercepted prompt produced a command result instead of a
        turn: the command's text is the whole answer."""
        part = TextPart(content=text, provider_name=DEEPSEEK_PROVIDER_NAME)
        index = self.take_part_index()
        yield PartStartEvent(index=index, part=part)
        yield PartEndEvent(index=index, part=part)
        self.result_text = text
        self.turn_started = True
        self.turn_ended = True
        self.messages.append(
            ModelResponse(
                parts=[part],
                provider_name=DEEPSEEK_PROVIDER_NAME,
                finish_reason="stop",
                metadata=deepseek_metadata(self.take_message_events([])),
            )
        )

    def take_message_events(self, events: list[JsonValue]) -> list[JsonValue]:
        message_events = [*self.pending_events, *events]
        self.pending_events = []
        return message_events
