from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace

from pydantic_ai import AgentRunResult
from pydantic_ai._agent_graph import GraphAgentState
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import (
    FinishReason,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequest,
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
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage, RunUsage

from octomate.capabilities.harness.events import StreamEvents
from octomate.tentacles.zcode import wire

ZCODE_FINISH_REASON_MAP: dict[str, FinishReason] = {
    "stop": "stop",
    "end_turn": "stop",
    "tool-calls": "tool_call",
    "length": "length",
    "max_tokens": "length",
    "content-filter": "content_filter",
    "error": "error",
}


@dataclass
class StreamingPart:
    index: int
    part: TextPart | ThinkingPart


@dataclass
class StreamingTool:
    call: ToolCallPart
    response: ModelResponse
    emitted: bool = False
    completed: bool = False


class ZcodeRunAccumulator:
    """Stream one input, then reconcile its ledger with ZCode's canonical messages."""

    def __init__(self, prompt: str, *, input_id: str, model: str) -> None:
        self.input_id: str = input_id
        self.model: str = model
        self.messages: list[ModelMessage] = [
            ModelRequest(parts=[UserPromptPart(prompt)])
        ]
        self.turn_id: str | None = None
        self.prompt_message_id: str | None = None
        self.seen: set[str] = set()
        self.responses: dict[str, ModelResponse] = {}
        self.tools: dict[str, StreamingTool] = {}
        self.current: StreamingPart | None = None
        self.current_message_id: str = ""
        self.part_index: int = 0
        self.ended: bool = False
        self.error: str | None = None
        self.result_text: str = ""
        self.usage: RunUsage = RunUsage()

    def response_for(self, message_id: str) -> ModelResponse:
        response = self.responses.get(message_id)
        if response is None:
            response = ModelResponse(
                parts=[],
                model_name=self.model,
                provider_name="zcode",
                provider_response_id=message_id or None,
            )
            self.responses[message_id] = response
            self.messages.append(response)
        return response

    def close_part(self) -> Iterator[StreamEvents[str]]:
        if self.current is not None:
            yield PartEndEvent(index=self.current.index, part=self.current.part)
            self.current = None

    def consume(self, event: wire.RunEvent) -> Iterator[StreamEvents[str]]:
        if self.ended or event.event_id in self.seen:
            return
        self.seen.add(event.event_id)
        if isinstance(event, wire.TurnStartedEvent):
            if event.payload.input_id != self.input_id:
                return
            if self.turn_id is not None and self.turn_id != event.turn_id:
                raise AgentRunError("ZCode started multiple turns for one input")
            self.turn_id = event.turn_id
            self.prompt_message_id = event.payload.message_id
            return
        if self.turn_id is None or event.turn_id != self.turn_id:
            return
        match event:
            case wire.ModelStreamingEvent(payload=chunk):
                yield from self.accumulate_content(chunk)
            case wire.ToolUpdatedEvent(payload=update):
                yield from self.update_tool(update)
            case wire.TurnCompletedEvent(payload=completed):
                self.result_text = completed.response
                self.ended = True
                if completed.result_type != "success":
                    self.error = f"ZCode turn ended: {completed.result_type}"
                usage = completed.usage
                self.usage = RunUsage(
                    requests=usage.model_request_count,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    details={"reasoning_tokens": usage.reasoning_tokens},
                )
                yield from self.close_part()
            case wire.TurnFailedEvent(payload=failed):
                self.error = failed.error.message
                self.ended = True
                yield from self.close_part()

    def accumulate_content(
        self, chunk: wire.ModelStreaming
    ) -> Iterator[StreamEvents[str]]:
        match chunk:
            case wire.ContentDelta():
                if not chunk.delta:
                    return
                message_id = chunk.assistant_message_id or self.current_message_id
                part_type = TextPart if chunk.kind == "text_delta" else ThinkingPart
                if (
                    self.current is None
                    or not isinstance(self.current.part, part_type)
                    or message_id != self.current_message_id
                ):
                    yield from self.close_part()
                    part = part_type(content="", provider_name="zcode")
                    self.current = StreamingPart(self.part_index, part)
                    self.part_index += 1
                    response = self.response_for(message_id)
                    response.parts = [*response.parts, part]
                    # The web stream queues events; its start must not share the mutable ledger part.
                    yield PartStartEvent(index=self.current.index, part=replace(part))
                self.current_message_id = message_id
                self.current.part.content += chunk.delta
                if isinstance(self.current.part, TextPart):
                    delta = TextPartDelta(chunk.delta)
                    self.result_text = self.current.part.content
                else:
                    delta = ThinkingPartDelta(content_delta=chunk.delta)
                yield PartDeltaEvent(index=self.current.index, delta=delta)
            case wire.ToolInputStart() | wire.ToolCall():
                tool = self.tools.get(chunk.tool_call_id)
                if tool is None:
                    if chunk.tool_name is None:
                        raise AgentRunError(
                            "ZCode emitted an unknown tool call without its name"
                        )
                    tool = StreamingTool(
                        ToolCallPart(
                            chunk.tool_name, args="", tool_call_id=chunk.tool_call_id
                        ),
                        self.response_for(
                            chunk.assistant_message_id or self.current_message_id
                        ),
                    )
                    self.tools[chunk.tool_call_id] = tool
                if isinstance(chunk, wire.ToolCall):
                    if chunk.input is not None:
                        tool.call.args = chunk.input
                    yield from self.emit_tool(tool)
            case wire.ToolInputDelta():
                tool = self.tools.get(chunk.tool_call_id)
                if tool is None or not isinstance(tool.call.args, str):
                    raise AgentRunError(
                        "ZCode emitted tool input without a matching start"
                    )
                tool.call.args += chunk.delta

    def update_tool(self, update: wire.ToolEvent) -> Iterator[StreamEvents[str]]:
        if isinstance(update, wire.ToolProgress) or update.source == "subagent":
            return
        tool = self.tools.get(update.tool_call_id)
        if isinstance(update, wire.ToolScheduled):
            if tool is None:
                tool = StreamingTool(
                    ToolCallPart(update.tool_name, tool_call_id=update.tool_call_id),
                    self.response_for(self.current_message_id),
                )
                self.tools[update.tool_call_id] = tool
            if update.input is not None:
                tool.call.args = update.input
            yield from self.emit_tool(tool)
            return
        if tool is None:
            raise AgentRunError("ZCode completed a tool without a matching call")
        if tool.completed:
            return
        yield from self.emit_tool(tool)
        if isinstance(update, wire.ToolFailed):
            failed = True
            output = update.error.message
        else:
            failed = not update.result.success
            output = update.result.output
            if failed and update.result.error is not None:
                output = (
                    update.result.error.message
                    if isinstance(update.result.error, wire.ErrorDetail)
                    else update.result.error
                )
        tool.completed = True
        tool.response.parts = [
            *tool.response.parts,
            NativeToolReturnPart(
                tool_name=tool.call.tool_name,
                tool_call_id=tool.call.tool_call_id,
                content=output,
                provider_name="zcode",
                outcome="failed" if failed else "success",
            ),
        ]
        yield FunctionToolResultEvent(
            ToolReturnPart(
                tool_name=tool.call.tool_name,
                tool_call_id=tool.call.tool_call_id,
                content=output,
                outcome="failed" if failed else "success",
            )
        )

    def emit_tool(self, tool: StreamingTool) -> Iterator[StreamEvents[str]]:
        if not tool.emitted:
            yield from self.close_part()
            tool.emitted = True
            tool.response.parts = [
                *tool.response.parts,
                NativeToolCallPart(
                    tool_name=tool.call.tool_name,
                    args=tool.call.args,
                    tool_call_id=tool.call.tool_call_id,
                    provider_name="zcode",
                ),
            ]
            yield FunctionToolCallEvent(tool.call)

    def reconcile(
        self, history: wire.SessionMessages, *, after_message_id: str | None = None
    ) -> None:
        boundary = next(
            (
                index
                for index, message in enumerate(history.messages)
                if message.info.message_id == self.prompt_message_id
            ),
            None,
        )
        if boundary is None:
            if (
                self.ended
                and not history.messages
                and (
                    self.prompt_message_id is None
                    or (not self.responses and not self.usage.requests)
                )
            ):
                # Control commands and hook-blocked prompts can complete without persisting input.
                self.messages = self.messages[:1]
                if self.result_text:
                    self.messages.append(
                        ModelResponse(
                            parts=[TextPart(self.result_text)],
                            model_name=self.model,
                            provider_name="zcode",
                            finish_reason="error" if self.error else "stop",
                        )
                    )
                return
            # session/messages silently returns all history when its cursor is unknown.
            raise AgentRunError(
                "ZCode history omitted the current prompt; refusing to replay older turns"
            )
        if after_message_id is not None and any(
            message.info.message_id == after_message_id or message.info.visible
            for message in history.messages[:boundary]
        ):
            raise AgentRunError("ZCode history cursor is missing or was not honored")
        messages = self.messages[:1]
        for message in history.messages[boundary + 1 :]:
            info = message.info
            if not info.visible:
                continue
            if isinstance(info, wire.UserMessageInfo):
                user_text = [
                    part.text
                    for part in message.parts
                    if isinstance(part, wire.TextPart)
                    and not part.synthetic
                    and not part.ignored
                ]
                if user_text:
                    messages.append(
                        ModelRequest(parts=[UserPromptPart("\n".join(user_text))])
                    )
                continue
            if info.finish == "stream_recovery_discarded":
                continue
            finish_reason: FinishReason | None = None
            if info.error is not None:
                finish_reason = "error"
            elif info.finish is not None:
                finish_reason = ZCODE_FINISH_REASON_MAP.get(info.finish)
            response = ModelResponse(
                parts=[],
                model_name=info.model_id,
                provider_name="zcode",
                provider_response_id=info.message_id,
                usage=RequestUsage(
                    input_tokens=info.tokens.input,
                    output_tokens=info.tokens.output,
                    cache_read_tokens=info.tokens.cache.read,
                    cache_write_tokens=info.tokens.cache.write,
                    details={"reasoning_tokens": info.tokens.reasoning},
                ),
                finish_reason=finish_reason,
                provider_details={
                    "finish": info.finish,
                    **({"error": info.error.model_dump()} if info.error else {}),
                },
            )
            for part in message.parts:
                if isinstance(part, wire.TextPart):
                    if not part.synthetic and not part.ignored:
                        response.parts = [*response.parts, TextPart(part.text)]
                elif isinstance(part, wire.ReasoningPart):
                    response.parts = [
                        *response.parts,
                        ThinkingPart(part.text, provider_name="zcode"),
                    ]
                elif isinstance(part, wire.ToolPart):
                    response.parts = [
                        *response.parts,
                        NativeToolCallPart(
                            tool_name=part.tool,
                            args=part.state.input,
                            tool_call_id=part.call_id,
                            provider_name="zcode",
                        ),
                    ]
                    if isinstance(part.state, (wire.CompletedTool, wire.FailedTool)):
                        failed = isinstance(part.state, wire.FailedTool)
                        response.parts = [
                            *response.parts,
                            NativeToolReturnPart(
                                tool_name=part.tool,
                                tool_call_id=part.call_id,
                                content=part.state.error
                                if isinstance(part.state, wire.FailedTool)
                                else part.state.output,
                                outcome="failed" if failed else "success",
                                provider_name="zcode",
                            ),
                        ]
            if response.parts:
                messages.append(response)
        self.messages = messages

    def build_result(self, *, run_id: str, conversation_id: str) -> AgentRunResult[str]:
        return AgentRunResult(
            output=self.result_text,
            _state=GraphAgentState(
                message_history=self.messages,
                usage=self.usage,
                run_id=run_id,
                conversation_id=conversation_id,
            ),
        )
