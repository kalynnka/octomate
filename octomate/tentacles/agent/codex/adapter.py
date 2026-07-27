from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal, TypeAlias, TypeVar

from openai_codex.generated.v2_all import (
    AbsolutePathBuf,
    AgentMessageDeltaNotification,
    AgentMessageThreadItem,
    CommandExecutionOutputDeltaNotification,
    CommandExecutionStatus,
    CommandExecutionThreadItem,
    ConfigWarningNotification,
    DynamicToolCallStatus,
    DynamicToolCallThreadItem,
    ErrorNotification,
    FileChangeOutputDeltaNotification,
    FileChangePatchUpdatedNotification,
    FileChangeThreadItem,
    ItemCompletedNotification,
    ItemStartedNotification,
    MessagePhase,
    McpToolCallProgressNotification,
    McpToolCallStatus,
    McpToolCallThreadItem,
    PatchApplyStatus,
    PlanDeltaNotification,
    PlanThreadItem,
    ReasoningSummaryTextDeltaNotification,
    ReasoningThreadItem,
    ReasoningTextDeltaNotification,
    ThreadTokenUsage,
    ThreadTokenUsageUpdatedNotification,
    TurnPlanUpdatedNotification,
    TurnStatus,
    TurnCompletedNotification,
    WebSearchThreadItem,
)
from openai_codex.models import Notification, UnknownNotification
from pydantic import BaseModel, TypeAdapter
from pydantic_ai import AgentRunResult

# GraphAgentState is private pydantic-ai internals. A Codex run produces no real
# pydantic-ai graph state, so we synthesize the minimal state an AgentRunResult
# needs (message history + run/conversation id); pinned with the SDK version.
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
from octomate.types.json import JsonObject, JsonValue

ToolOutcome: TypeAlias = Literal["success", "failed", "denied"]
StructuredOutputT = TypeVar("StructuredOutputT")

CODEX_PROVIDER_NAME = "openai_codex"
CODEX_METADATA_SOURCE = "codex"
CODEX_FINISH_REASON_MAP: dict[TurnStatus, FinishReason] = {
    TurnStatus.completed: "stop",
    TurnStatus.interrupted: "stop",
    TurnStatus.failed: "error",
}

json_object_adapter = TypeAdapter(JsonObject)

# Codex thread items that are genuine tool activity, rendered as tool call/return.
# Everything else — agent/user messages, reasoning, plans, and unknown items — is
# content or telemetry, so it is never turned into a fabricated tool.
TOOL_ITEM_TYPES: tuple[type[BaseModel], ...] = (
    CommandExecutionThreadItem,
    FileChangeThreadItem,
    McpToolCallThreadItem,
    DynamicToolCallThreadItem,
    WebSearchThreadItem,
)


@dataclass
class StreamingPartState:
    index: int
    part: TextPart | ThinkingPart
    events: list[JsonValue] = field(default_factory=list)


@dataclass
class NativeToolState:
    live_call: ToolCallPart
    native_call: NativeToolCallPart
    output: str = ""
    events: list[JsonValue] = field(default_factory=list)


def map_usage(token_usage: ThreadTokenUsage) -> RequestUsage:
    usage = token_usage.last
    details = {
        "reasoning_output_tokens": usage.reasoning_output_tokens,
        "total_tokens": usage.total_tokens,
    }
    if token_usage.model_context_window is not None:
        details["model_context_window"] = token_usage.model_context_window
    return RequestUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cached_input_tokens,
        details=details,
    )


def notification_event(notification: Notification) -> JsonObject:
    payload = notification.payload
    if isinstance(payload, UnknownNotification):
        payload_dump = payload.params
    elif isinstance(payload, BaseModel):
        payload_dump = json_object_adapter.validate_python(
            payload.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
                warnings=False,
            )
        )
    else:
        payload_dump = {}
    return {"method": notification.method, "payload": payload_dump}


def codex_metadata(events: list[JsonValue] | None = None) -> JsonObject:
    metadata: JsonObject = {"source": CODEX_METADATA_SOURCE}
    if events:
        metadata["events"] = events
    return metadata


def command_args(item: CommandExecutionThreadItem) -> JsonObject:
    cwd = item.cwd.root if isinstance(item.cwd, AbsolutePathBuf) else str(item.cwd)
    return {
        "command": item.command,
        "cwd": cwd,
        "status": item.status.value,
    }


def command_outcome(status: CommandExecutionStatus) -> ToolOutcome:
    if status == CommandExecutionStatus.failed:
        return "failed"
    if status == CommandExecutionStatus.declined:
        return "denied"
    return "success"


def patch_outcome(status: PatchApplyStatus) -> ToolOutcome:
    if status == PatchApplyStatus.failed:
        return "failed"
    if status == PatchApplyStatus.declined:
        return "denied"
    return "success"


def mcp_outcome(status: McpToolCallStatus) -> ToolOutcome:
    if status == McpToolCallStatus.failed:
        return "failed"
    return "success"


def dynamic_tool_outcome(status: DynamicToolCallStatus) -> ToolOutcome:
    if status == DynamicToolCallStatus.failed:
        return "failed"
    return "success"


def model_json(value: BaseModel) -> JsonObject:
    return json_object_adapter.validate_python(
        value.model_dump(by_alias=True, exclude_none=True, mode="json", warnings=False)
    )


def thread_item_id(item: BaseModel) -> str:
    """The Codex thread item's own id — every thread item carries a required `id`,
    so tool calls/returns correlate by it. Fail fast rather than fall back to a
    non-unique name, which would conflate two concurrent items of the same type."""
    raw = model_json(item).get("id")
    if not isinstance(raw, str) or not raw:
        raise ValueError(
            f"Codex thread item {type(item).__name__} has no string id to correlate"
        )
    return raw


def tool_name_for_item(item: BaseModel) -> str:
    if isinstance(item, FileChangeThreadItem):
        return "codex_file_change"
    if isinstance(item, McpToolCallThreadItem):
        return "codex_mcp_tool_call"
    if isinstance(item, DynamicToolCallThreadItem):
        return "codex_dynamic_tool_call"
    if isinstance(item, WebSearchThreadItem):
        return "codex_web_search"
    return f"codex_{model_json(item).get('type', type(item).__name__)}"


class CodexRunAccumulator:
    """Translate a Codex SDK notification stream into Octomate run projections."""

    def __init__(self) -> None:
        self.messages: list[ModelMessage] = []
        self.usage = RunUsage()
        self.result_text = ""
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.turn_status: TurnStatus | None = None
        self.turn_error: str | None = None
        self.request_usage: RequestUsage | None = None
        self.part_index = 0
        self.streaming_parts: dict[str, StreamingPartState] = {}
        self.native_tools: dict[str, NativeToolState] = {}
        self.pending_events: list[JsonValue] = []
        self.usage_counted = False

    def begin(self, user_prompt: str | Sequence[UserContent] | None) -> None:
        if user_prompt:
            self.messages.append(
                PydanticModelRequest(
                    parts=[UserPromptPart(content=user_prompt)],
                    metadata=codex_metadata(),
                )
            )

    def consume(self, notification: Notification) -> Iterator[StreamEvents[str]]:
        event = notification_event(notification)
        payload = notification.payload
        if isinstance(payload, AgentMessageDeltaNotification):
            yield from self.consume_agent_message_delta(payload, event)
        elif isinstance(
            payload,
            ReasoningTextDeltaNotification | ReasoningSummaryTextDeltaNotification,
        ):
            yield from self.consume_reasoning_delta(payload, event)
        elif isinstance(payload, ItemStartedNotification):
            yield from self.consume_item_started(payload, event)
        elif isinstance(payload, CommandExecutionOutputDeltaNotification):
            self.consume_tool_output_delta(payload.item_id, payload.delta, event)
        elif isinstance(payload, FileChangeOutputDeltaNotification):
            self.consume_tool_output_delta(payload.item_id, payload.delta, event)
        elif isinstance(payload, FileChangePatchUpdatedNotification):
            self.consume_file_change_patch_updated(payload, event)
        elif isinstance(payload, McpToolCallProgressNotification):
            self.consume_tool_output_delta(payload.item_id, payload.message, event)
        elif isinstance(payload, PlanDeltaNotification):
            yield from self.consume_plan_delta(payload, event)
        elif isinstance(payload, ItemCompletedNotification):
            yield from self.consume_item_completed(payload, event)
        elif isinstance(payload, ErrorNotification):
            self.consume_error(payload, event)
        elif isinstance(payload, TurnPlanUpdatedNotification | ConfigWarningNotification):
            self.pending_events.append(event)
        elif isinstance(payload, ThreadTokenUsageUpdatedNotification):
            self.thread_id = payload.thread_id
            self.turn_id = payload.turn_id
            self.request_usage = map_usage(payload.token_usage)
            self.pending_events.append(event)
        elif isinstance(payload, TurnCompletedNotification):
            self.consume_turn_completed(payload, event)
        else:
            self.pending_events.append(event)

    def build_result(self, run_id: str, conversation_id: str) -> AgentRunResult[str]:
        state = GraphAgentState(
            message_history=self.messages,
            usage=self.usage,
            run_id=run_id,
            conversation_id=conversation_id,
        )
        return AgentRunResult(output=self.result_text, _state=state)

    def build_structured_result(
        self,
        output_adapter: TypeAdapter[StructuredOutputT],
        *,
        run_id: str,
        conversation_id: str,
    ) -> AgentRunResult[StructuredOutputT]:
        state = GraphAgentState(
            message_history=self.messages,
            usage=self.usage,
            run_id=run_id,
            conversation_id=conversation_id,
        )
        return AgentRunResult(
            output=output_adapter.validate_json(self.result_text),
            _state=state,
        )

    def take_part_index(self) -> int:
        index = self.part_index
        self.part_index += 1
        return index

    def consume_agent_message_delta(
        self, payload: AgentMessageDeltaNotification, event: JsonObject
    ) -> Iterator[StreamEvents[str]]:
        self.thread_id = payload.thread_id
        self.turn_id = payload.turn_id
        state = self.streaming_parts.get(payload.item_id)
        if state is None:
            part = TextPart(
                content="",
                id=payload.item_id,
                provider_name=CODEX_PROVIDER_NAME,
            )
            state = StreamingPartState(index=self.take_part_index(), part=part)
            self.streaming_parts[payload.item_id] = state
            yield PartStartEvent(index=state.index, part=part)
        state.events.append(event)
        if isinstance(state.part, TextPart):
            state.part.content += payload.delta
            self.result_text += payload.delta
            yield PartDeltaEvent(
                index=state.index,
                delta=TextPartDelta(
                    payload.delta,
                    provider_name=CODEX_PROVIDER_NAME,
                ),
            )

    def consume_reasoning_delta(
        self,
        payload: ReasoningTextDeltaNotification | ReasoningSummaryTextDeltaNotification,
        event: JsonObject,
    ) -> Iterator[StreamEvents[str]]:
        self.thread_id = payload.thread_id
        self.turn_id = payload.turn_id
        state = self.streaming_parts.get(payload.item_id)
        if state is None:
            part = ThinkingPart(
                content="",
                id=payload.item_id,
                provider_name=CODEX_PROVIDER_NAME,
            )
            state = StreamingPartState(index=self.take_part_index(), part=part)
            self.streaming_parts[payload.item_id] = state
            yield PartStartEvent(index=state.index, part=part)
        state.events.append(event)
        if isinstance(state.part, ThinkingPart):
            state.part.content += payload.delta
            yield PartDeltaEvent(
                index=state.index,
                delta=ThinkingPartDelta(
                    content_delta=payload.delta,
                    provider_name=CODEX_PROVIDER_NAME,
                ),
            )

    def consume_plan_delta(
        self, payload: PlanDeltaNotification, event: JsonObject
    ) -> Iterator[StreamEvents[str]]:
        self.thread_id = payload.thread_id
        self.turn_id = payload.turn_id
        state = self.streaming_parts.get(payload.item_id)
        if state is None:
            part = ThinkingPart(
                content="",
                id=payload.item_id,
                provider_name=CODEX_PROVIDER_NAME,
            )
            state = StreamingPartState(index=self.take_part_index(), part=part)
            self.streaming_parts[payload.item_id] = state
            yield PartStartEvent(index=state.index, part=part)
        state.events.append(event)
        if isinstance(state.part, ThinkingPart):
            state.part.content += payload.delta
            yield PartDeltaEvent(
                index=state.index,
                delta=ThinkingPartDelta(
                    content_delta=payload.delta,
                    provider_name=CODEX_PROVIDER_NAME,
                ),
            )

    def consume_item_started(
        self, payload: ItemStartedNotification, event: JsonObject
    ) -> Iterator[StreamEvents[str]]:
        self.thread_id = payload.thread_id
        self.turn_id = payload.turn_id
        item = payload.item.root
        if isinstance(item, CommandExecutionThreadItem):
            live_call = ToolCallPart(
                tool_name="codex_command_execution",
                args=command_args(item),
                tool_call_id=item.id,
                provider_name=CODEX_PROVIDER_NAME,
            )
            native_call = NativeToolCallPart(
                tool_name=live_call.tool_name,
                args=live_call.args,
                tool_call_id=item.id,
                provider_name=CODEX_PROVIDER_NAME,
            )
            self.native_tools[item.id] = NativeToolState(
                live_call=live_call,
                native_call=native_call,
                events=[event],
            )
            yield FunctionToolCallEvent(part=live_call)
            return
        if isinstance(item, PlanThreadItem):
            part = ThinkingPart(
                content=item.text,
                id=item.id,
                provider_name=CODEX_PROVIDER_NAME,
            )
            self.streaming_parts[item.id] = StreamingPartState(
                index=self.take_part_index(),
                part=part,
                events=[event],
            )
            yield PartStartEvent(index=self.streaming_parts[item.id].index, part=part)
            return
        if isinstance(item, TOOL_ITEM_TYPES):
            yield from self.start_native_tool(item, event)
            return
        # Agent/user messages, reasoning, and unknown items are not tools: their text
        # or thinking arrives via the delta/completion handlers, so just keep the raw
        # event for replay metadata rather than fabricating a tool call.
        self.pending_events.append(event)

    def start_native_tool(
        self, item: BaseModel, event: JsonObject
    ) -> Iterator[StreamEvents[str]]:
        args = model_json(item)
        live_call = ToolCallPart(
            tool_name=tool_name_for_item(item),
            args=args,
            tool_call_id=thread_item_id(item),
            provider_name=CODEX_PROVIDER_NAME,
        )
        native_call = NativeToolCallPart(
            tool_name=live_call.tool_name,
            args=live_call.args,
            tool_call_id=live_call.tool_call_id,
            provider_name=CODEX_PROVIDER_NAME,
        )
        self.native_tools[live_call.tool_call_id] = NativeToolState(
            live_call=live_call,
            native_call=native_call,
            events=[event],
        )
        yield FunctionToolCallEvent(part=live_call)

    def consume_tool_output_delta(
        self,
        item_id: str,
        delta: str,
        event: JsonObject,
    ) -> None:
        state = self.native_tools.get(item_id)
        if state is None:
            self.pending_events.append(event)
            return
        state.output += delta
        state.events.append(event)

    def consume_file_change_patch_updated(
        self,
        payload: FileChangePatchUpdatedNotification,
        event: JsonObject,
    ) -> None:
        self.thread_id = payload.thread_id
        self.turn_id = payload.turn_id
        state = self.native_tools.get(payload.item_id)
        if state is None:
            self.pending_events.append(event)
            return
        state.events.append(event)

    def consume_error(self, payload: ErrorNotification, event: JsonObject) -> None:
        self.thread_id = payload.thread_id
        self.turn_id = payload.turn_id
        self.turn_error = payload.error.message
        self.pending_events.append(event)

    def complete_native_tool(
        self, item: BaseModel, event: JsonObject, *, outcome: ToolOutcome
    ) -> Iterator[StreamEvents[str]]:
        args = model_json(item)
        item_id = thread_item_id(item)
        state = self.native_tools.pop(item_id, None)
        if state is None:
            state = NativeToolState(
                live_call=ToolCallPart(
                    tool_name=tool_name_for_item(item),
                    args=args,
                    tool_call_id=item_id,
                    provider_name=CODEX_PROVIDER_NAME,
                ),
                native_call=NativeToolCallPart(
                    tool_name=tool_name_for_item(item),
                    args=args,
                    tool_call_id=item_id,
                    provider_name=CODEX_PROVIDER_NAME,
                ),
            )
            yield FunctionToolCallEvent(part=state.live_call)
        state.events.append(event)
        content = state.output or model_json(item)
        live_return = ToolReturnPart(
            tool_name=state.live_call.tool_name,
            content=content,
            tool_call_id=item_id,
            outcome=outcome,
        )
        native_return = NativeToolReturnPart(
            tool_name=state.live_call.tool_name,
            content=content,
            tool_call_id=item_id,
            provider_name=CODEX_PROVIDER_NAME,
            outcome=outcome,
        )
        yield FunctionToolResultEvent(part=live_return)
        self.messages.append(
            ModelResponse(
                parts=[state.native_call, native_return],
                provider_name=CODEX_PROVIDER_NAME,
                provider_response_id=item_id,
                metadata=codex_metadata(self.take_message_events(state.events)),
            )
        )

    def consume_item_completed(
        self, payload: ItemCompletedNotification, event: JsonObject
    ) -> Iterator[StreamEvents[str]]:
        self.thread_id = payload.thread_id
        self.turn_id = payload.turn_id
        item = payload.item.root
        if isinstance(item, AgentMessageThreadItem):
            yield from self.complete_agent_message(item, event)
        elif isinstance(item, ReasoningThreadItem):
            yield from self.complete_reasoning(item, event)
        elif isinstance(item, CommandExecutionThreadItem):
            yield from self.complete_command(item, event)
        elif isinstance(item, PlanThreadItem):
            yield from self.complete_plan(item, event)
        elif isinstance(item, FileChangeThreadItem):
            yield from self.complete_native_tool(item, event, outcome=patch_outcome(item.status))
        elif isinstance(item, McpToolCallThreadItem):
            yield from self.complete_native_tool(item, event, outcome=mcp_outcome(item.status))
        elif isinstance(item, DynamicToolCallThreadItem):
            yield from self.complete_native_tool(
                item,
                event,
                outcome=dynamic_tool_outcome(item.status),
            )
        elif isinstance(item, WebSearchThreadItem):
            yield from self.complete_native_tool(item, event, outcome="success")
        else:
            # User-message echoes and any other non-tool item: preserve the raw event
            # in metadata, but don't fabricate a tool call/return — they are messages.
            self.pending_events.append(event)

    def complete_agent_message(
        self, item: AgentMessageThreadItem, event: JsonObject
    ) -> Iterator[StreamEvents[str]]:
        state = self.streaming_parts.pop(item.id, None)
        if state is None or not isinstance(state.part, TextPart):
            part = TextPart(
                content=item.text,
                id=item.id,
                provider_name=CODEX_PROVIDER_NAME,
            )
            state = StreamingPartState(index=self.take_part_index(), part=part)
            yield PartStartEvent(index=state.index, part=part)
        else:
            state.part.content = item.text
        state.events.append(event)
        if item.phase == MessagePhase.final_answer or item.phase is None:
            self.result_text = item.text
        yield PartEndEvent(index=state.index, part=state.part)
        self.messages.append(
            ModelResponse(
                parts=[state.part],
                provider_name=CODEX_PROVIDER_NAME,
                provider_response_id=item.id,
                metadata=codex_metadata(self.take_message_events(state.events)),
            )
        )

    def complete_reasoning(
        self, item: ReasoningThreadItem, event: JsonObject
    ) -> Iterator[StreamEvents[str]]:
        state = self.streaming_parts.pop(item.id, None)
        reasoning = "\n".join([*(item.content or []), *(item.summary or [])])
        streamed = (
            state.part.content
            if state is not None and isinstance(state.part, ThinkingPart)
            else ""
        )
        content = reasoning or streamed
        if not content:
            # Codex emits a reasoning item even when it carries no reasoning text
            # (summaries disabled, or a short turn with no streamed reasoning). Don't
            # surface an empty thinking block; keep the raw event for replay.
            self.pending_events.append(event)
            return
        if state is None or not isinstance(state.part, ThinkingPart):
            part = ThinkingPart(
                content=content,
                id=item.id,
                provider_name=CODEX_PROVIDER_NAME,
            )
            state = StreamingPartState(index=self.take_part_index(), part=part)
            yield PartStartEvent(index=state.index, part=part)
        else:
            state.part.content = content
        state.events.append(event)
        yield PartEndEvent(index=state.index, part=state.part)
        self.messages.append(
            ModelResponse(
                parts=[state.part],
                provider_name=CODEX_PROVIDER_NAME,
                provider_response_id=item.id,
                metadata=codex_metadata(self.take_message_events(state.events)),
            )
        )

    def complete_plan(
        self, item: PlanThreadItem, event: JsonObject
    ) -> Iterator[StreamEvents[str]]:
        state = self.streaming_parts.pop(item.id, None)
        if state is None or not isinstance(state.part, ThinkingPart):
            part = ThinkingPart(
                content=item.text,
                id=item.id,
                provider_name=CODEX_PROVIDER_NAME,
            )
            state = StreamingPartState(index=self.take_part_index(), part=part)
            yield PartStartEvent(index=state.index, part=part)
        else:
            state.part.content = item.text or state.part.content
        state.events.append(event)
        yield PartEndEvent(index=state.index, part=state.part)
        self.messages.append(
            ModelResponse(
                parts=[state.part],
                provider_name=CODEX_PROVIDER_NAME,
                provider_response_id=item.id,
                metadata=codex_metadata(self.take_message_events(state.events)),
            )
        )

    def complete_command(
        self, item: CommandExecutionThreadItem, event: JsonObject
    ) -> Iterator[StreamEvents[str]]:
        state = self.native_tools.pop(item.id, None)
        if state is None:
            live_call = ToolCallPart(
                tool_name="codex_command_execution",
                args=command_args(item),
                tool_call_id=item.id,
                provider_name=CODEX_PROVIDER_NAME,
            )
            state = NativeToolState(
                live_call=live_call,
                native_call=NativeToolCallPart(
                    tool_name=live_call.tool_name,
                    args=live_call.args,
                    tool_call_id=item.id,
                    provider_name=CODEX_PROVIDER_NAME,
                ),
            )
            yield FunctionToolCallEvent(part=live_call)
        state.events.append(event)
        content = item.aggregated_output if item.aggregated_output is not None else state.output
        live_return = ToolReturnPart(
            tool_name=state.live_call.tool_name,
            content=content,
            tool_call_id=item.id,
            outcome=command_outcome(item.status),
        )
        native_return = NativeToolReturnPart(
            tool_name=state.live_call.tool_name,
            content=content,
            tool_call_id=item.id,
            provider_name=CODEX_PROVIDER_NAME,
            outcome=live_return.outcome,
        )
        yield FunctionToolResultEvent(part=live_return)
        self.messages.append(
            ModelResponse(
                parts=[state.native_call, native_return],
                provider_name=CODEX_PROVIDER_NAME,
                provider_response_id=item.id,
                metadata=codex_metadata(self.take_message_events(state.events)),
            )
        )

    def consume_turn_completed(
        self, payload: TurnCompletedNotification, event: JsonObject
    ) -> None:
        self.thread_id = payload.thread_id
        self.turn_id = payload.turn.id
        self.turn_status = payload.turn.status
        if payload.turn.error is not None:
            self.turn_error = payload.turn.error.message
        self.pending_events.append(event)
        if self.request_usage is not None and not self.usage_counted:
            self.usage.incr(self.request_usage)
            self.usage.requests += 1
            for message in reversed(self.messages):
                if isinstance(message, ModelResponse):
                    message.usage = self.request_usage
                    break
            self.usage_counted = True
        for message in reversed(self.messages):
            if isinstance(message, ModelResponse):
                message.finish_reason = CODEX_FINISH_REASON_MAP.get(payload.turn.status)
                provider_details = dict(message.provider_details or {})
                provider_details["turn_status"] = payload.turn.status.value
                if payload.turn.error is not None:
                    provider_details["error"] = payload.turn.error.message
                message.provider_details = provider_details
                break
        else:
            if payload.turn.status == TurnStatus.failed:
                provider_details: JsonObject = {"turn_status": payload.turn.status.value}
                if payload.turn.error is not None:
                    provider_details["error"] = payload.turn.error.message
                self.messages.append(
                    ModelResponse(
                        parts=[
                            TextPart(
                                content=payload.turn.error.message
                                if payload.turn.error is not None
                                else "Codex turn failed",
                                provider_name=CODEX_PROVIDER_NAME,
                            )
                        ],
                        provider_name=CODEX_PROVIDER_NAME,
                        provider_response_id=payload.turn.id,
                        usage=self.request_usage or RequestUsage(),
                        finish_reason="error",
                        provider_details=provider_details,
                        metadata=codex_metadata(self.take_message_events([])),
                    )
                )

    def take_message_events(self, events: list[JsonValue]) -> list[JsonValue]:
        message_events = [*self.pending_events, *events]
        self.pending_events = []
        return message_events
