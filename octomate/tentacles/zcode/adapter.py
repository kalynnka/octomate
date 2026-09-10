from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace

from pydantic_ai import AgentRunResult
from pydantic_ai._agent_graph import GraphAgentState
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import (
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

    def consume(self, event: wire.SessionEvent) -> Iterator[StreamEvents[str]]:
        if self.ended or event.event_id in self.seen:
            return
        self.seen.add(event.event_id)
        if event.type == "turn.started":
            started = wire.TurnStarted.model_validate(event.payload)
            if started.input_id != self.input_id:
                return
            if self.turn_id is not None and self.turn_id != event.turn_id:
                raise AgentRunError("ZCode started multiple turns for one input")
            if event.turn_id is None:
                raise AgentRunError("ZCode turn.started omitted its turn ID")
            self.turn_id = event.turn_id
            self.prompt_message_id = started.message_id
            return
        if self.turn_id is None or event.turn_id != self.turn_id:
            return
        if event.type == "model.streaming":
            chunk = wire.ModelStreaming.model_validate(event.payload)
            message_id = chunk.assistant_message_id or self.current_message_id
            if chunk.kind in {"text_delta", "reasoning_delta"} and chunk.delta:
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
            elif chunk.tool_call_id is not None:
                tool = self.tools.get(chunk.tool_call_id)
                if tool is None and chunk.tool_name:
                    tool = StreamingTool(
                        ToolCallPart(
                            chunk.tool_name, args="", tool_call_id=chunk.tool_call_id
                        ),
                        self.response_for(message_id),
                    )
                    self.tools[chunk.tool_call_id] = tool
                if tool is not None:
                    if chunk.kind == "tool_input_delta" and isinstance(
                        tool.call.args, str
                    ):
                        tool.call.args += chunk.delta
                    if isinstance(chunk.input, dict):
                        tool.call.args = chunk.input
                    if chunk.kind == "tool_call":
                        yield from self.emit_tool(tool)
        elif event.type == "tool.updated":
            update = wire.ToolEvent.model_validate(event.payload)
            if update.source == "subagent" or update.tool_call_id is None:
                return
            tool = self.tools.get(update.tool_call_id)
            if update.kind == "scheduled":
                if tool is None:
                    if update.tool_name is None:
                        raise AgentRunError("ZCode scheduled a tool without its name")
                    tool = StreamingTool(
                        ToolCallPart(
                            update.tool_name, tool_call_id=update.tool_call_id
                        ),
                        self.response_for(self.current_message_id),
                    )
                    self.tools[update.tool_call_id] = tool
                if isinstance(update.input, (dict, str)):
                    tool.call.args = update.input
                yield from self.emit_tool(tool)
            elif (
                update.kind in {"result", "error"}
                and tool is not None
                and not tool.completed
            ):
                yield from self.emit_tool(tool)
                failed = update.kind == "error"
                output = ""
                if update.result is not None:
                    failed = not update.result.success
                    output = update.result.output
                    if failed and update.result.error is not None:
                        output = (
                            update.result.error.message
                            if isinstance(update.result.error, wire.ErrorDetail)
                            else update.result.error
                        )
                elif update.error is not None:
                    output = update.error.message
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
        elif event.type == "turn.completed":
            completed = wire.TurnCompleted.model_validate(event.payload)
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
        elif event.type == "turn.failed":
            self.error = wire.TurnFailed.model_validate(event.payload).error.message
            self.ended = True
            yield from self.close_part()

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

    def reconcile(self, history: wire.SessionMessages) -> None:
        if self.prompt_message_id is None:
            raise AgentRunError("ZCode history has no current prompt boundary")
        boundary = next(
            (
                index
                for index, message in enumerate(history.messages)
                if message.info.message_id == self.prompt_message_id
            ),
            None,
        )
        if boundary is None:
            # session/messages silently returns all history when its cursor is unknown.
            raise AgentRunError(
                "ZCode history omitted the current prompt; refusing to replay older turns"
            )
        messages = self.messages[:1]
        for message in history.messages[boundary + 1 :]:
            info = message.info
            if isinstance(info, wire.UserMessageInfo):
                user_text = [
                    wire.TextPart.model_validate(part).text
                    for part in message.parts
                    if part.get("type") == "text"
                ]
                if user_text:
                    messages.append(
                        ModelRequest(parts=[UserPromptPart("\n".join(user_text))])
                    )
                continue
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
                finish_reason="tool_call" if info.finish == "tool-calls" else "stop",
            )
            for raw in message.parts:
                if raw.get("type") not in {"text", "reasoning", "tool"}:
                    continue
                part = wire.message_part_adapter.validate_python(raw)
                if isinstance(part, wire.TextPart):
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
