from __future__ import annotations

import base64
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar, cast

from anthropic.types import Message as AnthropicMessage
from anthropic.types import TextBlock as AnthropicTextBlock
from anthropic.types import ThinkingBlock as AnthropicThinkingBlock
from anthropic.types import ToolUseBlock as AnthropicToolUseBlock
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import ContentBlock, Message
from pydantic import TypeAdapter
from pydantic_ai import AgentRunResult

# GraphAgentState is private pydantic-ai internals. A Claude run produces no real
# pydantic-ai graph state, so we synthesize the minimal state an AgentRunResult
# needs (message history + run/conversation id); pinned with the SDK version.
from pydantic_ai._agent_graph import GraphAgentState
from pydantic_ai.messages import (
    BinaryContent,
    FinishReason,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequest,
    ModelRequestPart,
    ModelResponse,
    ModelResponsePart,
    NativeToolCallPart,
    NativeToolReturnPart,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.usage import RequestUsage, RunUsage

from octomate.capabilities.harness.events import StreamEvents
from octomate.tentacles.agents.claude.transcript import (
    TranscriptAssistantLine,
    TranscriptUserLine,
    TranscriptUserMessage,
)

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


def normalize_tool_result_content(
    content: str | list[dict[str, Any]] | None,
) -> str | list[str | BinaryContent | dict[str, Any]]:
    """Normalize a Claude tool result into pydantic-ai's tool-return shape.

    A string result stays a string. A block list (Anthropic text/image blocks)
    becomes a list of `str` and `BinaryContent`, so a forked pydantic-ai agent and
    the replay renderer see real files instead of a raw base64 dict dumped into the
    result text. Blocks that aren't plain text or a base64 image are kept verbatim.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    items: list[str | BinaryContent | dict[str, Any]] = []
    for block in content:
        source = block.get("source")
        if block.get("type") == "text":
            items.append(str(block.get("text", "")))
        elif (
            block.get("type") == "image"
            and isinstance(source, dict)
            and source.get("type") == "base64"
        ):
            items.append(
                BinaryContent(
                    data=base64.b64decode(source.get("data", "")),
                    media_type=str(
                        source.get("media_type", "application/octet-stream")
                    ),
                )
            )
        else:
            items.append(block)
    return items


def assistant_message_from_transcript(message: AnthropicMessage) -> AssistantMessage:
    """The SDK `AssistantMessage` `consume` expects, built from a transcript's typed
    anthropic message — so a session replayed off disk feeds the same translation a
    live SDK run does. Text, thinking, and tool-use blocks carry over; block kinds a
    replay does not need (redacted thinking, server tools) are dropped."""
    content: list[ContentBlock] = []
    for block in message.content:
        if isinstance(block, AnthropicTextBlock):
            content.append(TextBlock(text=block.text))
        elif isinstance(block, AnthropicThinkingBlock):
            content.append(
                ThinkingBlock(thinking=block.thinking, signature=block.signature)
            )
        elif isinstance(block, AnthropicToolUseBlock):
            content.append(
                ToolUseBlock(
                    id=block.id,
                    name=block.name,
                    input=cast("dict[str, Any]", block.input),
                )
            )
    return AssistantMessage(
        content=content,
        model=message.model,
        usage=message.usage.model_dump(),
        stop_reason=message.stop_reason,
        message_id=message.id,
    )


def user_message_from_transcript(message: TranscriptUserMessage) -> UserMessage:
    """The SDK `UserMessage` `consume` expects, built from a transcript's typed user
    message. Only tool-result blocks matter to the run timeline (a plain prompt is
    seeded via `begin`, not consumed), so other content is left out."""
    content = message.content
    if isinstance(content, str):
        return UserMessage(content=content)
    blocks: list[ContentBlock] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            blocks.append(
                ToolResultBlock(
                    tool_use_id=str(block.get("tool_use_id", "")),
                    content=cast(
                        "str | list[dict[str, Any]] | None", block.get("content")
                    ),
                    is_error=cast("bool | None", block.get("is_error")),
                )
            )
    return UserMessage(content=blocks)


@dataclass
class StreamingBlock:
    """A text/thinking part being streamed live from Claude partial messages.

    `index` is the pydantic-ai part index used across its `PartStart`/`PartDelta`/
    `PartEnd` events; `part` accumulates the delta text so its `.content` matches
    the block Claude finalizes."""

    index: int
    part: TextPart | ThinkingPart


@dataclass
class ClaudeRunAccumulator:
    """Translates a Claude Agent SDK message stream into Octomate's two
    projections of a run, so a routed Claude run renders in the channel exactly
    like a native one:

    - **live stream events** — pydantic-ai `PartStart`/`PartDelta`/`PartEnd` +
      tool call/result events the channel feelers/timeline render as the run
      proceeds (proxying the Claude run to Slack/Lark/web). Ephemeral, never
      persisted.
    - **messages** — the run as pydantic-ai `ModelMessage`s, persisted via
      `record_agent_run`; the reply and its web replay render from these.

    With `include_partial_messages`, Claude streams the raw Anthropic
    `content_block_*` events, so text and thinking render token-by-token via
    `StreamEvent`s; the completed `AssistantMessage` then supplies persistence
    (and renders tool calls, which are not streamed). Tool results arrive in later
    `UserMessage`s and are correlated back to their call by `tool_use_id` to
    recover the tool name pydantic-ai's `ToolReturnPart` requires.
    """

    messages: list[ModelMessage] = field(default_factory=list)
    # Aggregated like a native pydantic-ai run: each response's RequestUsage summed in,
    # one request counted per response. Surfaces via AgentRunResult.usage().
    usage: RunUsage = field(default_factory=RunUsage)
    result_text: str = ""
    session_id: str | None = None
    # Set when the run was driven with an `output_format` schema: the SDK's
    # validated structured result (a JSON-able object), used by
    # `build_structured_result` instead of the freeform text.
    structured_output: object | None = None
    tool_names: dict[str, str] = field(default_factory=dict)
    part_index: int = 0
    # In-flight streamed parts for the current message, keyed by Anthropic
    # content-block index.
    streaming_blocks: dict[int, StreamingBlock] = field(default_factory=dict)
    # Set once any text/thinking block has streamed via partials. While true, the
    # completed `AssistantMessage` persists those blocks but does not re-render
    # them (partials already did) — robust to however the SDK interleaves partial
    # and complete messages.
    streaming_active: bool = False

    def begin(self, user_prompt: str | Sequence[UserContent] | None) -> None:
        if user_prompt:
            self.messages.append(
                ModelRequest(parts=[UserPromptPart(content=user_prompt)])
            )

    def consume(
        self, message: Message | TranscriptAssistantLine | TranscriptUserLine
    ) -> Iterator[StreamEvents[str]]:
        """Translate one message into the run's projections. Accepts both the SDK's
        live message stream and a transcript's typed lines (transcript.py) — a replay
        off disk converts to the SDK shape and shares this one translation."""
        if isinstance(message, TranscriptAssistantLine):
            yield from self._consume_assistant(
                assistant_message_from_transcript(message.message)
            )
        elif isinstance(message, TranscriptUserLine):
            yield from self._consume_tool_results(
                user_message_from_transcript(message.message)
            )
        elif isinstance(message, StreamEvent):
            yield from self._consume_stream_event(message)
        elif isinstance(message, AssistantMessage):
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
            usage=self.usage,
            run_id=run_id,
            conversation_id=conversation_id,
        )
        return AgentRunResult(output=self.result_text, _state=state)

    def build_deferred_result(
        self, requests: DeferredToolRequests, *, run_id: str, conversation_id: str
    ) -> AgentRunResult[DeferredToolRequests]:
        """The run as it ended: interrupted for a deferral the graph resumes."""
        state = GraphAgentState(
            message_history=self.messages,
            usage=self.usage,
            run_id=run_id,
            conversation_id=conversation_id,
        )
        return AgentRunResult(output=requests, _state=state)

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
            usage=self.usage,
            run_id=run_id,
            conversation_id=conversation_id,
        )
        return AgentRunResult(output=output, _state=state)

    def _take_index(self) -> int:
        index = self.part_index
        self.part_index += 1
        return index

    def _consume_stream_event(
        self, message: StreamEvent
    ) -> Iterator[StreamEvents[str]]:
        """Render Claude's raw Anthropic `content_block_*` stream as live token
        deltas. Text/thinking blocks emit `PartStart`/`PartDelta`/`PartEnd` here;
        the completed `AssistantMessage` then persists them without re-rendering
        (tracked in `streamed_indices`). Tool-use blocks are left to the completed
        message, which renders them as function-tool calls."""
        event = message.event
        event_type = event.get("type")
        if event_type == "message_start":
            # A fresh assistant message: its content-block indices restart at 0.
            self.streaming_blocks.clear()
            return
        if event_type == "content_block_start":
            index = event.get("index")
            block = event.get("content_block") or {}
            block_type = block.get("type")
            if not isinstance(index, int):
                return
            # Start empty and let the deltas fill the part; the completed message
            # supplies the authoritative content for persistence. Seeding from the
            # start event would double-count the first chunk in the live view.
            if block_type == "text":
                part: TextPart | ThinkingPart = TextPart(content="")
            elif block_type == "thinking":
                part = ThinkingPart(content="", provider_name=CLAUDE_PROVIDER_NAME)
            else:
                return
            self.streaming_active = True
            stream_index = self._take_index()
            self.streaming_blocks[index] = StreamingBlock(index=stream_index, part=part)
            yield PartStartEvent(index=stream_index, part=part)
            return
        if event_type == "content_block_delta":
            index = event.get("index")
            state = self.streaming_blocks.get(index) if isinstance(index, int) else None
            if state is None:
                return
            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta" and isinstance(state.part, TextPart):
                text = str(delta.get("text", ""))
                state.part.content += text
                yield PartDeltaEvent(
                    index=state.index,
                    delta=TextPartDelta(text, provider_name=CLAUDE_PROVIDER_NAME),
                )
            elif delta_type == "thinking_delta" and isinstance(
                state.part, ThinkingPart
            ):
                text = str(delta.get("thinking", ""))
                state.part.content += text
                yield PartDeltaEvent(
                    index=state.index,
                    delta=ThinkingPartDelta(
                        content_delta=text, provider_name=CLAUDE_PROVIDER_NAME
                    ),
                )
            elif delta_type == "signature_delta" and isinstance(
                state.part, ThinkingPart
            ):
                yield PartDeltaEvent(
                    index=state.index,
                    delta=ThinkingPartDelta(
                        signature_delta=str(delta.get("signature", "")),
                        provider_name=CLAUDE_PROVIDER_NAME,
                    ),
                )
            return
        if event_type == "content_block_stop":
            index = event.get("index")
            if not isinstance(index, int):
                return
            state = self.streaming_blocks.pop(index, None)
            if state is not None:
                yield PartEndEvent(index=state.index, part=state.part)

    def _consume_assistant(
        self, message: AssistantMessage
    ) -> Iterator[StreamEvents[str]]:
        parts: list[ModelResponsePart] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                text = TextPart(content=block.text)
                parts.append(text)
                self.result_text = block.text
                # Partials already streamed the text live; this pass only persists
                # it. Only render here when partials are off (the fallback path).
                if not self.streaming_active:
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
                if not self.streaming_active:
                    index = self._take_index()
                    yield PartStartEvent(index=index, part=thinking)
                    yield PartEndEvent(index=index, part=thinking)
            elif isinstance(block, ToolUseBlock):
                call = ToolCallPart(
                    tool_name=block.name,
                    args=block.input,
                    tool_call_id=block.id,
                )
                parts.append(call)
                self.tool_names[block.id] = block.name
                yield FunctionToolCallEvent(part=call)
            elif isinstance(block, ServerToolUseBlock):
                # Server-side tools (advisor, web_search, …) are run by the API, so
                # they persist as native parts a forked pydantic-ai agent reads as
                # already-executed rather than as a pending function call. The live
                # event stays the function-tool event the channels already render.
                self.tool_names[block.id] = block.name
                parts.append(
                    NativeToolCallPart(
                        tool_name=block.name,
                        args=block.input,
                        tool_call_id=block.id,
                        provider_name=CLAUDE_PROVIDER_NAME,
                    )
                )
                yield FunctionToolCallEvent(
                    part=ToolCallPart(
                        tool_name=block.name,
                        args=block.input,
                        tool_call_id=block.id,
                    )
                )
            elif isinstance(block, ServerToolResultBlock):
                # A server tool's result rides in the same assistant message as its
                # call, so it belongs to this ModelResponse — not a later ModelRequest.
                name = self.tool_names.get(block.tool_use_id, "")
                parts.append(
                    NativeToolReturnPart(
                        tool_name=name,
                        content=block.content,
                        tool_call_id=block.tool_use_id,
                        provider_name=CLAUDE_PROVIDER_NAME,
                    )
                )
                yield FunctionToolResultEvent(
                    part=ToolReturnPart(
                        tool_name=name,
                        content=block.content,
                        tool_call_id=block.tool_use_id,
                    )
                )
        if parts:
            provider_details: dict[str, str] = {}
            if message.stop_reason:
                provider_details["finish_reason"] = message.stop_reason
            if message.parent_tool_use_id:
                provider_details["parent_tool_use_id"] = message.parent_tool_use_id
            if message.error:
                provider_details["error"] = message.error
            response_usage = map_usage(message.usage)
            # Match native pydantic-ai run accounting (see _agent_graph): sum each
            # response's usage into the run usage, one request counted per response.
            self.usage.incr(response_usage)
            self.usage.requests += 1
            self.messages.append(
                ModelResponse(
                    parts=parts,
                    model_name=message.model,
                    usage=response_usage,
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
                        content=normalize_tool_result_content(block.content),
                        tool_call_id=block.tool_use_id,
                    )
                parts.append(part)
                yield FunctionToolResultEvent(part=part)
        if parts:
            self.messages.append(ModelRequest(parts=parts))
