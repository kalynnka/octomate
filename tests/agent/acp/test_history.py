"""ACP content keeps its meaning in both live events and stored messages."""

import acp
import pytest
from acp import schema
from pydantic_ai.messages import (
    BinaryContent,
    FilePart,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
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

from octomate.tentacles.acp.history import AcpHistory, ContentBlock, content_value


@pytest.mark.parametrize("window", [0, 128000])
def test_context_snapshot_is_independent_of_billing(window: int) -> None:
    history = AcpHistory(schema.PromptRequest(session_id="native", prompt=[]))
    for used in (100, 900):
        events = history.feed(
            schema.SessionNotification(
                session_id="native",
                update=schema.UsageUpdate(
                    session_update="usage_update", used=used, size=window
                ),
            )
        )
        assert events == []
    assert history.context_used == 900
    assert history.context_window == (window or None)
    assert history.response.usage.input_tokens == 0


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        (acp.text_block(" hello "), " hello "),
        (
            schema.ResourceContentBlock(
                type="resource_link", uri="file:///private/a", name="a"
            ),
            "a (file:///private/a)",
        ),
        (
            schema.EmbeddedResourceContentBlock(
                type="resource",
                resource=schema.TextResourceContents(
                    uri="file:///private/a", text="embedded"
                ),
            ),
            "embedded",
        ),
        (
            acp.image_block("aW1hZ2U=", "image/png"),
            BinaryContent(data=b"image", media_type="image/png"),
        ),
        (
            schema.AudioContentBlock(
                type="audio", data="YXVkaW8=", mime_type="audio/wav"
            ),
            BinaryContent(data=b"audio", media_type="audio/wav"),
        ),
        (
            schema.EmbeddedResourceContentBlock(
                type="resource",
                resource=schema.BlobResourceContents(
                    uri="file:///private/a", blob="YmxvYg=="
                ),
            ),
            BinaryContent(data=b"blob", media_type="application/octet-stream"),
        ),
    ],
)
def test_content_is_retained_without_fetching_resources(
    block: ContentBlock, expected: str | BinaryContent
) -> None:
    assert content_value(block) == expected
    history = AcpHistory(schema.PromptRequest(session_id="native", prompt=[block]))
    assert isinstance(history.request.parts[0], UserPromptPart)
    assert history.request.parts[0].content == [expected]
    assert history.request.metadata is not None
    assert history.request.metadata["source"] == "acp"


def test_stream_boundaries_and_event_snapshots() -> None:
    history = AcpHistory(schema.PromptRequest(session_id="native", prompt=[]))
    first = history.feed(
        schema.SessionNotification(
            session_id="native", update=acp.update_agent_message_text("hel")
        )
    )
    second = history.feed(
        schema.SessionNotification(
            session_id="native", update=acp.update_agent_message_text("lo")
        )
    )
    thought = history.feed(
        schema.SessionNotification(
            session_id="native", update=acp.update_agent_thought_text("consider")
        )
    )
    next_message = history.feed(
        schema.SessionNotification(
            session_id="native",
            update=schema.AgentMessageChunk(
                session_update="agent_message_chunk",
                content=acp.text_block("answer"),
                message_id="second",
            ),
        )
    )
    new_id = history.feed(
        schema.SessionNotification(
            session_id="native",
            update=schema.AgentMessageChunk(
                session_update="agent_message_chunk",
                content=acp.text_block("next"),
                message_id="third",
            ),
        )
    )
    assert isinstance(first[0], PartStartEvent)
    assert first[0].part == TextPart("hel")
    assert isinstance(second[0], PartDeltaEvent)
    assert isinstance(thought[0], PartEndEvent)
    assert thought[0].part == TextPart("hello")
    assert isinstance(next_message[0], PartEndEvent)
    assert next_message[0].part == ThinkingPart("consider")
    assert isinstance(new_id[0], PartEndEvent)
    assert new_id[0].part == TextPart("answer")
    assert history.answer == "hello\n\nanswer\n\nnext"
    assert len(history.messages) == 2
    assert len(history.updates) == 5
    assert len(history.end_part()) == 1
    assert history.end_part() == []


def test_tool_progress_keeps_inputs_raw_results_and_diff() -> None:
    history = AcpHistory(schema.PromptRequest(session_id="native", prompt=[]))
    started = history.feed(
        schema.SessionNotification(
            session_id="native",
            update=acp.start_tool_call(
                "edit", "Edit a", status="pending", raw_input={"path": "a"}
            ),
        )
    )
    finished = history.feed(
        schema.SessionNotification(
            session_id="native",
            update=acp.update_tool_call(
                "edit",
                status="failed",
                raw_output={"error": "denied"},
                content=[
                    schema.FileEditToolCallContent(
                        type="diff", path="a", old_text="before", new_text="after"
                    )
                ],
            ),
        )
    )
    assert isinstance(started[0], FunctionToolCallEvent)
    assert isinstance(finished[0], FunctionToolResultEvent)
    assert isinstance(finished[0].part, ToolReturnPart)
    assert finished[0].part.outcome == "failed"
    call, result = history.parts
    assert isinstance(call, NativeToolCallPart)
    assert call.args == {"path": "a"}
    assert call.provider_details is not None
    assert isinstance(result, NativeToolReturnPart)
    assert result.content == {"error": "denied"}
    assert history.tools["edit"].content == [
        schema.FileEditToolCallContent(
            type="diff", path="a", old_text="before", new_text="after"
        )
    ]
    assert history.answer == ""


def test_binary_reply_has_inline_data_and_ledger_label() -> None:
    history = AcpHistory(schema.PromptRequest(session_id="native", prompt=[]))
    events = history.feed(
        schema.SessionNotification(
            session_id="native",
            update=schema.AgentMessageChunk(
                session_update="agent_message_chunk",
                content=acp.image_block("aW1hZ2U=", "image/png"),
            ),
        )
    )
    assert len(events) == 2
    assert isinstance(history.parts[0], FilePart)
    assert history.parts[0].content.data == b"image"
    assert history.answer == "[image/png]"
