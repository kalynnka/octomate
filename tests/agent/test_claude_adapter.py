from __future__ import annotations

import base64

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
from pydantic_ai.messages import (
    BinaryContent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    PartEndEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from octomate.tentacles.agent.claude.adapter import (
    ClaudeRunAccumulator,
    map_usage,
    normalize_tool_result_content,
)


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


def test_adapter_run_usage_aggregates_like_a_native_run() -> None:
    acc = ClaudeRunAccumulator()
    acc.begin("go")
    list(
        acc.consume(
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Read", input={})],
                model="m",
                usage={"input_tokens": 100, "output_tokens": 10},
            )
        )
    )
    # A tool result is a ModelRequest, not a response — it must not count as a request.
    list(
        acc.consume(
            UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="ok")])
        )
    )
    list(
        acc.consume(
            AssistantMessage(
                content=[TextBlock(text="done")],
                model="m",
                usage={
                    "input_tokens": 120,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 40,
                },
            )
        )
    )

    # AgentRunResult.usage reports the aggregated RunUsage, exactly as a native
    # pydantic-ai run does — summed tokens, one request per model response.
    run_usage = acc.build_result(run_id="run-1", conversation_id="conv-1").usage
    assert run_usage.requests == 2
    assert run_usage.input_tokens == 220
    assert run_usage.output_tokens == 15
    assert run_usage.cache_read_tokens == 40


def test_adapter_fills_response_provenance_usage_and_signature() -> None:
    acc = ClaudeRunAccumulator()
    list(
        acc.consume(
            AssistantMessage(
                content=[
                    ThinkingBlock(thinking="hmm", signature="sig-xyz"),
                    TextBlock(text="done"),
                ],
                model="claude-opus-4-8",
                usage={
                    "input_tokens": 120,
                    "output_tokens": 34,
                    "cache_creation_input_tokens": 10,
                    "cache_read_input_tokens": 5,
                    "service_tier": "standard",  # non-int → ignored
                },
                message_id="msg_01ABC",
                stop_reason="end_turn",
                parent_tool_use_id="task_1",
            )
        )
    )

    turn = acc.messages[-1]
    assert isinstance(turn, ModelResponse)

    # Thinking signatures survive with their provider (required for round-trip).
    thinking = turn.parts[0]
    assert isinstance(thinking, ThinkingPart)
    assert thinking.signature == "sig-xyz"
    assert thinking.provider_name == "anthropic"

    # Response provenance is filled from the Claude message.
    assert turn.provider_name == "anthropic"
    assert turn.provider_response_id == "msg_01ABC"
    assert turn.finish_reason == "stop"
    assert turn.provider_details == {
        "finish_reason": "end_turn",
        "parent_tool_use_id": "task_1",
    }

    # Usage maps onto RequestUsage, Anthropic cache names included.
    assert turn.usage.input_tokens == 120
    assert turn.usage.output_tokens == 34
    assert turn.usage.cache_write_tokens == 10
    assert turn.usage.cache_read_tokens == 5
    assert turn.usage.details == {}


def test_normalize_tool_result_content_text_and_image() -> None:
    assert normalize_tool_result_content("hi") == "hi"
    assert normalize_tool_result_content(None) == ""

    png = base64.b64encode(b"PNGDATA").decode()
    items = normalize_tool_result_content(
        [
            {"type": "text", "text": "here is the screenshot"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": png},
            },
            {"type": "weird", "payload": 1},  # unknown block kept verbatim
        ]
    )
    assert isinstance(items, list)
    assert items[0] == "here is the screenshot"
    image = items[1]
    assert isinstance(image, BinaryContent)
    assert image.media_type == "image/png"
    assert image.data == b"PNGDATA"
    assert items[2] == {"type": "weird", "payload": 1}


def test_adapter_tool_result_image_becomes_binary_content() -> None:
    acc = ClaudeRunAccumulator()
    list(
        acc.consume(
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Read", input={})], model="m"
            )
        )
    )
    png = base64.b64encode(b"PNGDATA").decode()
    list(
        acc.consume(
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="t1",
                        content=[
                            {"type": "text", "text": "screenshot:"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": png,
                                },
                            },
                        ],
                    )
                ]
            )
        )
    )

    request = acc.messages[-1]
    assert isinstance(request, ModelRequest)
    ret = request.parts[0]
    assert isinstance(ret, ToolReturnPart)
    # The image is a real file part, not a base64 blob dumped into the result text.
    assert ret.model_response_str() == "screenshot:"
    assert len(ret.files) == 1
    assert isinstance(ret.files[0], BinaryContent)
    assert ret.files[0].data == b"PNGDATA"


def test_adapter_maps_server_tools_to_native_parts() -> None:
    acc = ClaudeRunAccumulator()
    events = list(
        acc.consume(
            AssistantMessage(
                content=[
                    ServerToolUseBlock(id="s1", name="web_search", input={"query": "x"}),
                    ServerToolResultBlock(tool_use_id="s1", content={"results": []}),
                    TextBlock(text="here you go"),
                ],
                model="claude-opus-4-8",
            )
        )
    )

    # Live stream still renders server tools through the function-tool events the
    # channels already handle (call + result), then the text part.
    assert [type(e) for e in events] == [
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        PartStartEvent,
        PartEndEvent,
    ]

    # Persisted parts are native — a fork reads them as already-executed, both in the
    # same ModelResponse (the result rides the assistant message, not a ModelRequest).
    turn = acc.messages[-1]
    assert isinstance(turn, ModelResponse)
    assert [type(p) for p in turn.parts] == [
        NativeToolCallPart,
        NativeToolReturnPart,
        TextPart,
    ]

    native_call = turn.parts[0]
    assert isinstance(native_call, NativeToolCallPart)
    assert native_call.tool_name == "web_search"
    assert native_call.tool_call_id == "s1"
    assert native_call.provider_name == "anthropic"

    native_return = turn.parts[1]
    assert isinstance(native_return, NativeToolReturnPart)
    assert native_return.tool_name == "web_search"  # recovered from the call by id
    assert native_return.tool_call_id == "s1"
    assert native_return.content == {"results": []}
    assert native_return.provider_name == "anthropic"


def test_map_usage_maps_cache_names_and_preserves_extra_counts() -> None:
    usage = map_usage(
        {
            "input_tokens": 7,
            "output_tokens": 3,
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 1,
            "some_new_count": 9,
            "service_tier": "standard",  # non-int → dropped
        }
    )
    assert usage.input_tokens == 7
    assert usage.output_tokens == 3
    assert usage.cache_write_tokens == 2
    assert usage.cache_read_tokens == 1
    # Unknown integer counts are preserved; already-mapped names don't double up.
    assert usage.details == {"some_new_count": 9}

    empty = map_usage(None)
    assert empty.input_tokens == 0 and empty.output_tokens == 0
    assert empty.cache_write_tokens == 0 and empty.cache_read_tokens == 0
