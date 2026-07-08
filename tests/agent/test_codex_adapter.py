from __future__ import annotations

from typing import cast

from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    CommandExecutionOutputDeltaNotification,
    CommandExecutionStatus,
    ErrorNotification,
    FileChangeOutputDeltaNotification,
    FileChangePatchUpdatedNotification,
    FileChangeThreadItem,
    ItemCompletedNotification,
    ItemStartedNotification,
    MessagePhase,
    McpToolCallProgressNotification,
    PlanDeltaNotification,
    ReasoningTextDeltaNotification,
    ThreadItem,
    ThreadTokenUsage,
    ThreadTokenUsageUpdatedNotification,
    TurnError,
    TurnItemsView,
    TokenUsageBreakdown,
    Turn,
    TurnCompletedNotification,
    TurnStatus,
)
from openai_codex.models import Notification, NotificationPayload
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    ThinkingPart,
    ToolReturnPart,
    UserPromptPart,
)

from octomate.tentacles.agent.codex.adapter import CodexRunAccumulator, map_usage


def notification(method: str, payload: NotificationPayload) -> Notification:
    return Notification(method=method, payload=payload)


def command_item(
    *,
    status: CommandExecutionStatus,
    aggregated_output: str | None = None,
    exit_code: int | None = None,
) -> ThreadItem:
    return ThreadItem.model_validate(
        {
            "id": "cmd-1",
            "command": "pytest",
            "commandActions": [],
            "cwd": "/repo",
            "status": status.value,
            "aggregatedOutput": aggregated_output,
            "exitCode": exit_code,
            "type": "commandExecution",
        }
    )


def file_change_item(*, status: str = "completed") -> ThreadItem:
    return ThreadItem.model_validate(
        {
            "id": "file-1",
            "changes": [
                {
                    "diff": "--- a/app.py\n+++ b/app.py\n@@\n-pass\n+print('ok')\n",
                    "kind": {"type": "update"},
                    "path": "app.py",
                }
            ],
            "status": status,
            "type": "fileChange",
        }
    )


def mcp_item(*, status: str = "completed") -> ThreadItem:
    return ThreadItem.model_validate(
        {
            "id": "mcp-1",
            "arguments": {"issue": 1},
            "result": {
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": {"ok": True},
            },
            "server": "github",
            "status": status,
            "tool": "issue_get",
            "type": "mcpToolCall",
        }
    )


def dynamic_tool_item(*, status: str = "completed") -> ThreadItem:
    return ThreadItem.model_validate(
        {
            "id": "dynamic-1",
            "arguments": {"path": "app.py"},
            "status": status,
            "success": status == "completed",
            "tool": "read_file",
            "type": "dynamicToolCall",
        }
    )


def web_search_item() -> ThreadItem:
    return ThreadItem.model_validate(
        {
            "id": "web-1",
            "query": "codex sdk",
            "action": {"query": "codex sdk", "type": "search"},
            "type": "webSearch",
        }
    )


def plan_item(text: str) -> ThreadItem:
    return ThreadItem.model_validate(
        {
            "id": "plan-1",
            "text": text,
            "type": "plan",
        }
    )


def token_breakdown(
    *, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
) -> TokenUsageBreakdown:
    return TokenUsageBreakdown(
        cached_input_tokens=cached_input_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_output_tokens=3,
        total_tokens=input_tokens + output_tokens,
    )


def test_adapter_maps_prompt_and_text_delta_to_messages_and_events() -> None:
    acc = CodexRunAccumulator()
    acc.begin("fix the test")

    events = list(
        acc.consume(
            notification(
                "item/agentMessage/delta",
                AgentMessageDeltaNotification(
                    delta="found ",
                    item_id="msg-1",
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )
    events += list(
        acc.consume(
            notification(
                "item/agentMessage/delta",
                AgentMessageDeltaNotification(
                    delta="it",
                    item_id="msg-1",
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )
    events += list(
        acc.consume(
            notification(
                "item/completed",
                ItemCompletedNotification(
                    completed_at_ms=1,
                    item=ThreadItem.model_validate(
                        {
                            "id": "msg-1",
                            "phase": MessagePhase.final_answer.value,
                            "text": "found it",
                            "type": "agentMessage",
                        }
                    ),
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )

    assert [type(event) for event in events] == [
        PartStartEvent,
        PartDeltaEvent,
        PartDeltaEvent,
        PartEndEvent,
    ]
    assert [type(message) for message in acc.messages] == [ModelRequest, ModelResponse]

    request = acc.messages[0]
    assert isinstance(request, ModelRequest)
    assert isinstance(request.parts[0], UserPromptPart)
    assert request.parts[0].content == "fix the test"

    response = acc.messages[1]
    assert isinstance(response, ModelResponse)
    text = response.parts[0]
    assert isinstance(text, TextPart)
    assert text.content == "found it"
    assert response.metadata is not None
    assert response.metadata["source"] == "codex"
    assert acc.result_text == "found it"


def test_adapter_maps_reasoning_delta_to_thinking_part() -> None:
    acc = CodexRunAccumulator()

    events = list(
        acc.consume(
            notification(
                "item/reasoning/delta",
                ReasoningTextDeltaNotification(
                    content_index=0,
                    delta="checking",
                    item_id="reasoning-1",
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )
    events += list(
        acc.consume(
            notification(
                "item/completed",
                ItemCompletedNotification(
                    completed_at_ms=1,
                    item=ThreadItem.model_validate(
                        {
                            "id": "reasoning-1",
                            "content": ["checking"],
                            "summary": [],
                            "type": "reasoning",
                        }
                    ),
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )

    assert [type(event) for event in events] == [
        PartStartEvent,
        PartDeltaEvent,
        PartEndEvent,
    ]
    [response] = acc.messages
    assert isinstance(response, ModelResponse)
    thinking = response.parts[0]
    assert isinstance(thinking, ThinkingPart)
    assert thinking.content == "checking"


def test_adapter_maps_command_item_to_native_tool_parts() -> None:
    acc = CodexRunAccumulator()

    events = list(
        acc.consume(
            notification(
                "item/started",
                ItemStartedNotification(
                    item=command_item(status=CommandExecutionStatus.in_progress),
                    started_at_ms=1,
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )
    events += list(
        acc.consume(
            notification(
                "item/commandExecution/outputDelta",
                CommandExecutionOutputDeltaNotification(
                    delta="ok",
                    item_id="cmd-1",
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )
    events += list(
        acc.consume(
            notification(
                "item/completed",
                ItemCompletedNotification(
                    completed_at_ms=2,
                    item=command_item(
                        status=CommandExecutionStatus.completed,
                        aggregated_output="ok",
                        exit_code=0,
                    ),
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )

    assert [type(event) for event in events] == [
        FunctionToolCallEvent,
        FunctionToolResultEvent,
    ]
    result = events[1]
    assert isinstance(result, FunctionToolResultEvent)
    assert isinstance(result.part, ToolReturnPart)
    assert result.part.content == "ok"
    assert result.part.outcome == "success"

    [response] = acc.messages
    assert isinstance(response, ModelResponse)
    native_call, native_return = response.parts
    assert isinstance(native_call, NativeToolCallPart)
    assert native_call.tool_name == "codex_command_execution"
    assert native_call.args_as_dict() == {
        "command": "pytest",
        "cwd": "/repo",
        "status": "inProgress",
    }
    assert isinstance(native_return, NativeToolReturnPart)
    assert native_return.content == "ok"
    assert native_return.outcome == "success"


def test_adapter_maps_file_change_item_to_native_tool_parts() -> None:
    acc = CodexRunAccumulator()

    events = list(
        acc.consume(
            notification(
                "item/started",
                ItemStartedNotification(
                    item=file_change_item(status="inProgress"),
                    started_at_ms=1,
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )
    list(
        acc.consume(
            notification(
                "item/fileChange/outputDelta",
                FileChangeOutputDeltaNotification(
                    delta="patch applied",
                    item_id="file-1",
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )
    list(
        acc.consume(
            notification(
                "item/fileChange/patchUpdated",
                cast(
                    NotificationPayload,
                    FileChangePatchUpdatedNotification(
                        changes=cast(
                            FileChangeThreadItem, file_change_item().root
                        ).changes,
                        item_id="file-1",
                        thread_id="thread-1",
                        turn_id="turn-1",
                    ),
                ),
            )
        )
    )
    events += list(
        acc.consume(
            notification(
                "item/completed",
                ItemCompletedNotification(
                    completed_at_ms=2,
                    item=file_change_item(),
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )

    assert [type(event) for event in events] == [
        FunctionToolCallEvent,
        FunctionToolResultEvent,
    ]
    [response] = acc.messages
    native_call, native_return = response.parts
    assert isinstance(native_call, NativeToolCallPart)
    assert native_call.tool_name == "codex_file_change"
    assert isinstance(native_return, NativeToolReturnPart)
    assert native_return.content == "patch applied"
    assert native_return.outcome == "success"


def test_adapter_maps_mcp_and_web_search_items_to_native_tools() -> None:
    acc = CodexRunAccumulator()

    mcp_events = list(
        acc.consume(
            notification(
                "item/started",
                ItemStartedNotification(
                    item=mcp_item(status="inProgress"),
                    started_at_ms=1,
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )
    list(
        acc.consume(
            notification(
                "item/mcpToolCall/progress",
                McpToolCallProgressNotification(
                    item_id="mcp-1",
                    message="reading",
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )
    mcp_events += list(
        acc.consume(
            notification(
                "item/completed",
                ItemCompletedNotification(
                    completed_at_ms=2,
                    item=mcp_item(),
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )
    web_events = list(
        acc.consume(
            notification(
                "item/completed",
                ItemCompletedNotification(
                    completed_at_ms=3,
                    item=web_search_item(),
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )

    assert [type(event) for event in mcp_events] == [
        FunctionToolCallEvent,
        FunctionToolResultEvent,
    ]
    assert [type(event) for event in web_events] == [
        FunctionToolCallEvent,
        FunctionToolResultEvent,
    ]
    mcp_response, web_response = acc.messages
    mcp_call, mcp_return = mcp_response.parts
    assert isinstance(mcp_call, NativeToolCallPart)
    assert mcp_call.tool_name == "codex_mcp_tool_call"
    assert isinstance(mcp_return, NativeToolReturnPart)
    assert mcp_return.content == "reading"
    web_call, web_return = web_response.parts
    assert isinstance(web_call, NativeToolCallPart)
    assert web_call.tool_name == "codex_web_search"
    assert isinstance(web_return, NativeToolReturnPart)
    assert web_return.outcome == "success"


def test_adapter_maps_dynamic_tool_items_to_native_tools() -> None:
    acc = CodexRunAccumulator()

    events = list(
        acc.consume(
            notification(
                "item/started",
                ItemStartedNotification(
                    item=dynamic_tool_item(status="inProgress"),
                    started_at_ms=1,
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )
    events += list(
        acc.consume(
            notification(
                "item/completed",
                ItemCompletedNotification(
                    completed_at_ms=2,
                    item=dynamic_tool_item(),
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )

    assert [type(event) for event in events] == [
        FunctionToolCallEvent,
        FunctionToolResultEvent,
    ]
    [response] = acc.messages
    dynamic_call, dynamic_return = response.parts
    assert isinstance(dynamic_call, NativeToolCallPart)
    assert dynamic_call.tool_name == "codex_dynamic_tool_call"
    assert isinstance(dynamic_return, NativeToolReturnPart)
    assert dynamic_return.outcome == "success"


def test_adapter_maps_plan_delta_to_thinking_part() -> None:
    acc = CodexRunAccumulator()

    events = list(
        acc.consume(
            notification(
                "item/plan/delta",
                PlanDeltaNotification(
                    delta="1. inspect",
                    item_id="plan-1",
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )
    events += list(
        acc.consume(
            notification(
                "item/completed",
                ItemCompletedNotification(
                    completed_at_ms=1,
                    item=plan_item("1. inspect\n2. patch"),
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )

    assert [type(event) for event in events] == [
        PartStartEvent,
        PartDeltaEvent,
        PartEndEvent,
    ]
    [response] = acc.messages
    thinking = response.parts[0]
    assert isinstance(thinking, ThinkingPart)
    assert thinking.content == "1. inspect\n2. patch"


def test_adapter_maps_usage_and_turn_completion() -> None:
    acc = CodexRunAccumulator()

    list(
        acc.consume(
            notification(
                "thread/tokenUsage/updated",
                ThreadTokenUsageUpdatedNotification(
                    thread_id="thread-1",
                    turn_id="turn-1",
                    token_usage=ThreadTokenUsage(
                        last=token_breakdown(
                            input_tokens=11,
                            output_tokens=7,
                            cached_input_tokens=5,
                        ),
                        total=token_breakdown(input_tokens=11, output_tokens=7),
                        model_context_window=200000,
                    ),
                ),
            )
        )
    )
    list(
        acc.consume(
            notification(
                "turn/completed",
                TurnCompletedNotification(
                    thread_id="thread-1",
                    turn=Turn(
                        id="turn-1",
                        items=[],
                        items_view=TurnItemsView.full,
                        status=TurnStatus.completed,
                    ),
                ),
            )
        )
    )

    assert acc.thread_id == "thread-1"
    assert acc.turn_id == "turn-1"
    assert acc.turn_status == TurnStatus.completed
    assert acc.usage.requests == 1
    assert acc.usage.input_tokens == 11
    assert acc.usage.output_tokens == 7
    assert acc.usage.cache_read_tokens == 5
    assert acc.usage.details == {
        "reasoning_output_tokens": 3,
        "total_tokens": 18,
        "model_context_window": 200000,
    }


def test_adapter_attaches_usage_and_finish_reason_to_last_response() -> None:
    acc = CodexRunAccumulator()
    list(
        acc.consume(
            notification(
                "item/completed",
                ItemCompletedNotification(
                    completed_at_ms=1,
                    item=ThreadItem.model_validate(
                        {
                            "id": "msg-1",
                            "phase": MessagePhase.final_answer.value,
                            "text": "done",
                            "type": "agentMessage",
                        }
                    ),
                    thread_id="thread-1",
                    turn_id="turn-1",
                ),
            )
        )
    )
    list(
        acc.consume(
            notification(
                "thread/tokenUsage/updated",
                ThreadTokenUsageUpdatedNotification(
                    thread_id="thread-1",
                    turn_id="turn-1",
                    token_usage=ThreadTokenUsage(
                        last=token_breakdown(input_tokens=3, output_tokens=4),
                        total=token_breakdown(input_tokens=3, output_tokens=4),
                    ),
                ),
            )
        )
    )
    list(
        acc.consume(
            notification(
                "turn/completed",
                TurnCompletedNotification(
                    thread_id="thread-1",
                    turn=Turn(
                        id="turn-1",
                        items=[],
                        items_view=TurnItemsView.full,
                        status=TurnStatus.completed,
                    ),
                ),
            )
        )
    )

    response = acc.messages[-1]
    assert isinstance(response, ModelResponse)
    assert response.usage.input_tokens == 3
    assert response.finish_reason == "stop"
    assert response.provider_details == {"turn_status": "completed"}


def test_adapter_persists_failed_turn_without_response_items() -> None:
    acc = CodexRunAccumulator()

    list(
        acc.consume(
            notification(
                "error",
                ErrorNotification(
                    error=TurnError(message="boom"),
                    thread_id="thread-1",
                    turn_id="turn-1",
                    will_retry=False,
                ),
            )
        )
    )
    list(
        acc.consume(
            notification(
                "turn/completed",
                TurnCompletedNotification(
                    thread_id="thread-1",
                    turn=Turn(
                        id="turn-1",
                        error=TurnError(message="boom"),
                        items=[],
                        items_view=TurnItemsView.full,
                        status=TurnStatus.failed,
                    ),
                ),
            )
        )
    )

    [response] = acc.messages
    assert isinstance(response, ModelResponse)
    assert response.finish_reason == "error"
    assert response.provider_details == {
        "turn_status": "failed",
        "error": "boom",
    }
    text = response.parts[0]
    assert isinstance(text, TextPart)
    assert text.content == "boom"


def test_map_usage_uses_last_usage_snapshot() -> None:
    usage = map_usage(
        ThreadTokenUsage(
            last=token_breakdown(input_tokens=2, output_tokens=1),
            total=token_breakdown(input_tokens=99, output_tokens=99),
        )
    )

    assert usage.input_tokens == 2
    assert usage.output_tokens == 1
