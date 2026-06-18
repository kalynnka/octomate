from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

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

from octomate.capabilities.events import StreamEvents


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

    def build_result(self, run_id: str, conversation_id: str) -> AgentRunResult[str]:
        state = GraphAgentState(
            message_history=self.messages,
            run_id=run_id,
            conversation_id=conversation_id,
        )
        return AgentRunResult(output=self.result_text, _state=state)

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
                thinking = ThinkingPart(content=block.thinking)
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
            self.messages.append(ModelResponse(parts=parts, model_name=message.model))

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
