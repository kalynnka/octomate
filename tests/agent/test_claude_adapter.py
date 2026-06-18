from __future__ import annotations

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    PartEndEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from octomate.tentacles.agent.claude.adapter import ClaudeRunAccumulator


def test_adapter_maps_a_full_turn_to_messages_and_events() -> None:
    acc = ClaudeRunAccumulator()
    acc.begin("find the bug")

    events = list(
        acc.consume(
            AssistantMessage(
                content=[
                    ThinkingBlock(thinking="let me look", signature="sig"),
                    TextBlock(text="checking auth.py"),
                    ToolUseBlock(id="t1", name="Read", input={"file_path": "auth.py"}),
                ],
                model="claude-opus-4-8",
            )
        )
    )
    events += list(
        acc.consume(
            UserMessage(
                content=[ToolResultBlock(tool_use_id="t1", content="def login(): ...")]
            )
        )
    )
    events += list(
        acc.consume(AssistantMessage(content=[TextBlock(text="found it")], model="m"))
    )
    events += list(
        acc.consume(
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="sess-1",
                result="found it",
            )
        )
    )

    # Live events for the channel feelers: thinking + text are start/end pairs,
    # tools are single call/result events.
    assert [type(e) for e in events] == [
        PartStartEvent,
        PartEndEvent,
        PartStartEvent,
        PartEndEvent,
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        PartStartEvent,
        PartEndEvent,
    ]

    # Persisted messages: user prompt, the assistant turn, the tool return, the
    # final assistant text.
    assert [type(m) for m in acc.messages] == [
        ModelRequest,
        ModelResponse,
        ModelRequest,
        ModelResponse,
    ]
    prompt = acc.messages[0]
    assert isinstance(prompt, ModelRequest)
    assert isinstance(prompt.parts[0], UserPromptPart)
    assert prompt.parts[0].content == "find the bug"

    turn = acc.messages[1]
    assert isinstance(turn, ModelResponse)
    assert [type(p) for p in turn.parts] == [ThinkingPart, TextPart, ToolCallPart]
    assert turn.model_name == "claude-opus-4-8"
    call = turn.parts[2]
    assert isinstance(call, ToolCallPart)
    assert call.tool_name == "Read" and call.tool_call_id == "t1"

    tool_return = acc.messages[2]
    assert isinstance(tool_return, ModelRequest)
    ret = tool_return.parts[0]
    assert isinstance(ret, ToolReturnPart)
    # Tool name is recovered from the call by tool_use_id.
    assert ret.tool_name == "Read" and ret.tool_call_id == "t1"
    assert ret.content == "def login(): ..."

    assert acc.result_text == "found it"
    assert acc.session_id == "sess-1"


def test_adapter_error_tool_result_becomes_retry_prompt() -> None:
    acc = ClaudeRunAccumulator()
    list(
        acc.consume(
            AssistantMessage(
                content=[ToolUseBlock(id="t9", name="Bash", input={"command": "x"})],
                model="m",
            )
        )
    )
    list(
        acc.consume(
            UserMessage(
                content=[
                    ToolResultBlock(tool_use_id="t9", content="boom", is_error=True)
                ]
            )
        )
    )
    request = acc.messages[-1]
    assert isinstance(request, ModelRequest)
    retry = request.parts[0]
    assert isinstance(retry, RetryPromptPart)
    assert retry.tool_name == "Bash" and retry.content == "boom"


def test_adapter_build_result_synthesizes_agent_run_result() -> None:
    acc = ClaudeRunAccumulator()
    acc.begin("hi")
    list(acc.consume(AssistantMessage(content=[TextBlock(text="hello")], model="m")))

    result = acc.build_result(run_id="run-1", conversation_id="conv-1")
    assert result.output == "hello"
    assert result.run_id == "run-1"
    assert result.all_messages() == acc.messages
