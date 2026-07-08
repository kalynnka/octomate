from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import TypeAdapter

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import Message
from pydantic_ai import AgentRunResult

# GraphAgentState is private pydantic-ai internals. A Claude run produces no real
# pydantic-ai graph state, so we synthesize the minimal state an AgentRunResult
# needs (message history + run/conversation id); pinned with the SDK version.
from pydantic_ai._agent_graph import GraphAgentState
from pydantic_ai.messages import (
    FinishReason,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequest,
    ModelRequestPart,
    ModelResponse,
    ModelResponsePart,
    PartEndEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage

from octomate.capabilities.events import StreamEvents

StructuredOutputT = TypeVar("StructuredOutputT")

# The Claude Agent SDK runs against Anthropic; naming the provider lets pydantic-ai
# round-trip the thinking `signature` and keep the provenance fields honest.
CLAUDE_PROVIDER_NAME = "anthropic"

# Claude stop reasons → pydantic-ai's OpenTelemetry-normalized FinishReason, mirroring
# pydantic_ai.models.anthropic._FINISH_REASON_MAP so a forked/replayed Claude run reads
# the same way a native pydantic-ai run does.
FINISH_REASON_MAP: dict[str, FinishReason] = {
    "compaction": "stop",
    "end_turn": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_call",
    "pause_turn": "stop",
    "refusal": "content_filter",
}


def map_usage(raw: dict[str, object] | None) -> RequestUsage:
    """Project a Claude usage block onto pydantic-ai's RequestUsage.

    Anthropic names its cache counts differently (`cache_creation`/`cache_read` →
    RequestUsage's `cache_write`/`cache_read`); any other integer counts are kept
    verbatim in `details` so nothing is silently dropped.
    """
    if not raw:
        return RequestUsage()
    counts = {key: value for key, value in raw.items() if isinstance(value, int)}
    primary = {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    }
    return RequestUsage(
        input_tokens=counts.get("input_tokens", 0),
        output_tokens=counts.get("output_tokens", 0),
        cache_write_tokens=counts.get("cache_creation_input_tokens", 0),
        cache_read_tokens=counts.get("cache_read_input_tokens", 0),
        details={key: value for key, value in counts.items() if key not in primary},
    )


@dataclass
class ClaudeRunAccumulator:
    """Translates a Claude Agent SDK message stream into Octomate's two
    projections of a run, so a routed Claude run renders in the channel exactly
    like a native one:

    - **live stream events** — pydantic-ai `PartStart`/`PartEnd` + tool
      call/result events the channel feelers/timeline render as the run proceeds
      (proxying the Claude run to Slack/Lark/web). Ephemeral, never persisted.
    - **messages** — the run as pydantic-ai `ModelMessage`s, persisted via
      `record_agent_run`; the reply and its web replay render from these.

    Claude emits whole content blocks (no token deltas), so each block maps to a
    single start/end event pair (text, thinking) or one tool call/result event.
    Tool results arrive in later `UserMessage`s and are correlated back to their
    call by `tool_use_id` to recover the tool name pydantic-ai's `ToolReturnPart`
    requires.
    """

    messages: list[ModelMessage] = field(default_factory=list)
    result_text: str = ""
    session_id: str | None = None
    # Set when the run was driven with an `output_format` schema: the SDK's
    # validated structured result (a JSON-able object), used by
    # `build_structured_result` instead of the freeform text.
    structured_output: object | None = None
    tool_names: dict[str, str] = field(default_factory=dict)
    part_index: int = 0

    def begin(self, user_prompt: str | Sequence[UserContent] | None) -> None:
        if user_prompt:
            self.messages.append(
                ModelRequest(parts=[UserPromptPart(content=user_prompt)])
            )

    def consume(self, message: Message) -> Iterator[StreamEvents[str]]:
        if isinstance(message, AssistantMessage):
            yield from self._consume_assistant(message)
        elif isinstance(message, UserMessage):
            yield from self._consume_tool_results(message)
        elif isinstance(message, ResultMessage):
            if message.session_id:
                self.session_id = message.session_id
            if message.result:
                self.result_text = message.result
            if message.structured_output is not None:
                self.structured_output = message.structured_output

    def build_result(self, run_id: str, conversation_id: str) -> AgentRunResult[str]:
        state = GraphAgentState(
            message_history=self.messages,
            run_id=run_id,
            conversation_id=conversation_id,
        )
        return AgentRunResult(output=self.result_text, _state=state)

    def build_structured_result(
        self,
        output_adapter: TypeAdapter[StructuredOutputT],
        run_id: str,
        conversation_id: str,
    ) -> AgentRunResult[StructuredOutputT]:
        # TODO: retry through the Claude agent on validation failure, matching
        # pydantic-ai's structured output repair loop.
        output = output_adapter.validate_python(self.structured_output)
        state = GraphAgentState(
            message_history=self.messages,
            run_id=run_id,
            conversation_id=conversation_id,
        )
        return AgentRunResult(output=output, _state=state)

    def _take_index(self) -> int:
        index = self.part_index
        self.part_index += 1
        return index

    def _consume_assistant(
        self, message: AssistantMessage
    ) -> Iterator[StreamEvents[str]]:
        parts: list[ModelResponsePart] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                text = TextPart(content=block.text)
                parts.append(text)
                self.result_text = block.text
                index = self._take_index()
                yield PartStartEvent(index=index, part=text)
                yield PartEndEvent(index=index, part=text)
            elif isinstance(block, ThinkingBlock):
                thinking = ThinkingPart(
                    content=block.thinking,
                    signature=block.signature,
                    provider_name=CLAUDE_PROVIDER_NAME,
                )
                parts.append(thinking)
                index = self._take_index()
                yield PartStartEvent(index=index, part=thinking)
                yield PartEndEvent(index=index, part=thinking)
            elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
                call = ToolCallPart(
                    tool_name=block.name,
                    args=block.input,
                    tool_call_id=block.id,
                )
                parts.append(call)
                self.tool_names[block.id] = block.name
                yield FunctionToolCallEvent(part=call)
        if parts:
            provider_details: dict[str, str] = {}
            if message.stop_reason:
                provider_details["finish_reason"] = message.stop_reason
            if message.parent_tool_use_id:
                provider_details["parent_tool_use_id"] = message.parent_tool_use_id
            if message.error:
                provider_details["error"] = message.error
            self.messages.append(
                ModelResponse(
                    parts=parts,
                    model_name=message.model,
                    usage=map_usage(message.usage),
                    provider_name=CLAUDE_PROVIDER_NAME,
                    provider_response_id=message.message_id,
                    finish_reason=(
                        FINISH_REASON_MAP.get(message.stop_reason)
                        if message.stop_reason
                        else None
                    ),
                    provider_details=provider_details or None,
                )
            )

    def _consume_tool_results(
        self, message: UserMessage
    ) -> Iterator[StreamEvents[str]]:
        if isinstance(message.content, str):
            return
        parts: list[ModelRequestPart] = []
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                name = self.tool_names.get(block.tool_use_id, "")
                if block.is_error:
                    part: ModelRequestPart = RetryPromptPart(
                        content=str(block.content) if block.content is not None else "",
                        tool_name=name or None,
                        tool_call_id=block.tool_use_id,
                    )
                else:
                    part = ToolReturnPart(
                        tool_name=name,
                        content=block.content if block.content is not None else "",
                        tool_call_id=block.tool_use_id,
                    )
                parts.append(part)
                yield FunctionToolResultEvent(part=part)
            elif isinstance(block, ServerToolResultBlock):
                part = ToolReturnPart(
                    tool_name=self.tool_names.get(block.tool_use_id, ""),
                    content=block.content,
                    tool_call_id=block.tool_use_id,
                )
                parts.append(part)
                yield FunctionToolResultEvent(part=part)
        if parts:
            self.messages.append(ModelRequest(parts=parts))
