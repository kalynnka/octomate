"""Message-text/role derivation and the history search + pagination capability."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import (
    BinaryContent,
    TextContent,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.messages import ModelRequest as RawModelRequest
from pydantic_ai.messages import ModelResponse as RawModelResponse
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.capabilities.history import HistoryCapability
from octomate.managers import ConversationManager
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.messages import ModelRequest, ModelResponse


def _send_args(*texts: str) -> dict[str, Any]:
    return {"segments": [{"type": "text", "data": {"text": t}} for t in texts]}


def _sent_text(*texts: str) -> str:
    """The message_text a text-only `send_message` yields: its raw args dict,
    `str()`'d — matching the projection."""
    return str(_send_args(*texts))


# --- message_text / role derivation (no database) ---


def test_request_user_prompt_str() -> None:
    message = ModelRequest(parts=[UserPromptPart(content="hello world")])
    assert message.role == "user"
    assert message.message_text == "hello world"


def test_request_user_prompt_list_skips_multimodal() -> None:
    message = ModelRequest(
        parts=[
            UserPromptPart(
                content=[
                    TextContent(content="line A"),
                    BinaryContent(data=b"x", media_type="image/png"),
                    "line B",
                ]
            )
        ]
    )
    assert message.role == "user"
    assert message.message_text == "line A\nline B"


def test_request_tool_return_only_is_assistant_without_text() -> None:
    message = ModelRequest(
        parts=[ToolReturnPart(tool_name="x", content="res", tool_call_id="c1")]
    )
    assert message.role == "assistant"
    assert message.message_text is None


def test_response_text() -> None:
    message = ModelResponse(parts=[TextPart(content="the answer")])
    assert message.role == "assistant"
    assert message.message_text == "the answer"


@pytest.mark.parametrize("as_json_str", [False, True])
def test_response_send_message_text(as_json_str: bool) -> None:
    args = _send_args("sent hi")
    payload: Any = json.dumps(args) if as_json_str else args
    message = ModelResponse(
        parts=[ToolCallPart(tool_name="send_message", args=payload, tool_call_id="c1")]
    )
    assert message.message_text == _sent_text("sent hi")


def test_response_send_stores_raw_segments_searchable() -> None:
    # message_text keeps the raw send args (each segment dict str()'d), so every
    # segment's content stays searchable — including non-text segment fields.
    segments = [
        {"type": "markdown", "data": {"text": "look at this"}},
        {"type": "image", "data": {"file": "/cat.png", "summary": "a cat"}},
    ]
    message = ModelResponse(
        parts=[
            ToolCallPart(
                tool_name="send_message",
                args={"segments": segments},
                tool_call_id="c1",
            )
        ]
    )
    assert message.message_text == str({"segments": segments})
    assert message.message_text is not None
    assert "look at this" in message.message_text
    assert "a cat" in message.message_text


def test_response_thinking_excluded() -> None:
    message = ModelResponse(parts=[ThinkingPart(content="hmm")])
    assert message.message_text is None


def test_response_text_and_send_combined_other_tool_excluded() -> None:
    message = ModelResponse(
        parts=[
            TextPart(content="answer"),
            ToolCallPart(
                tool_name="send_message", args=_send_args("update"), tool_call_id="c1"
            ),
            ToolCallPart(tool_name="other", args={"x": 1}, tool_call_id="c2"),
        ]
    )
    # answer (TextPart) + the send; the non-send tool call contributes nothing.
    assert message.message_text == "answer\n\n" + _sent_text("update")


# --- persistence + search/pagination (in-memory database) ---


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> AsyncIterator[None]:
    yield


def _key() -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="dev_ui",
        chat_type="private",
        chat_id="alice",
        user_id="dev",
    )


async def _seed(manager: ConversationManager) -> Any:
    """One run: user turn, an assistant turn that thinks then sends, the tool
    return for that send, and a final assistant answer. conversation_id is stamped
    on each message exactly as pydantic-ai does for a real run."""
    conversation = await manager.ensure(_key(), agent_tentacle_id="inkling")
    cid = str(conversation.id)
    run_id = "run-1"
    ts = datetime.now(timezone.utc)
    await manager.record_agent_run(
        conversation,
        run_id=run_id,
        messages=[
            RawModelRequest(
                parts=[UserPromptPart(content="find the auth bug")],
                run_id=run_id,
                conversation_id=cid,
                timestamp=ts,
            ),
            RawModelResponse(
                parts=[
                    ThinkingPart(content="secret reasoning"),
                    ToolCallPart(
                        tool_name="send_message",
                        args=_send_args("on it"),
                        tool_call_id="c1",
                    ),
                ],
                run_id=run_id,
                conversation_id=cid,
                timestamp=ts,
            ),
            RawModelRequest(
                parts=[ToolReturnPart(tool_name="x", content="r", tool_call_id="c1")],
                run_id=run_id,
                conversation_id=cid,
                timestamp=ts,
            ),
            RawModelResponse(
                parts=[TextPart(content="the bug is in login")],
                run_id=run_id,
                conversation_id=cid,
                timestamp=ts,
            ),
        ],
    )
    return conversation


async def test_record_run_persists_role_and_text(in_memory_engine: AsyncEngine) -> None:
    await _seed(ConversationManager())
    # Read raw columns straight from the table to prove they are stored, not
    # recomputed on the way out.
    async with in_memory_engine.connect() as conn:
        rows = (
            await conn.execute(
                sql_text(
                    "SELECT kind, role, message_text FROM model_messages ORDER BY id"
                )
            )
        ).all()
    assert [tuple(row) for row in rows] == [
        ("request", "user", "find the auth bug"),
        ("response", "assistant", _sent_text("on it")),
        ("request", "assistant", None),
        ("response", "assistant", "the bug is in login"),
    ]


async def test_search_messages_text_only_and_role_filter() -> None:
    manager = ConversationManager()
    conversation = await _seed(manager)

    hits = await manager.search_messages(conversation.id, "bug")
    assert [(m.role, m.message_text) for m in hits] == [
        ("user", "find the auth bug"),
        ("assistant", "the bug is in login"),
    ]
    assert [
        m.role
        for m in await manager.search_messages(conversation.id, "bug", role="user")
    ] == ["user"]
    assert [
        m.role
        for m in await manager.search_messages(conversation.id, "bug", role="assistant")
    ] == ["assistant"]
    # Thinking/tool-call content carries no message_text and is not searchable.
    assert await manager.search_messages(conversation.id, "secret") == []


async def test_pagination_includes_non_text_neighbours() -> None:
    manager = ConversationManager()
    conversation = await _seed(manager)
    anchor = (await manager.search_messages(conversation.id, "login"))[0]

    before = await manager.messages_before(conversation.id, anchor.id, limit=10)
    # The window recovers the send response and the text-less tool-return request.
    assert [(type(m).__name__, m.message_text) for m in before] == [
        ("ModelRequest", "find the auth bug"),
        ("ModelResponse", _sent_text("on it")),
        ("ModelRequest", None),
    ]
    assert [m.id for m in before] == sorted(m.id for m in before)
    assert await manager.messages_after(conversation.id, anchor.id, limit=10) == []


async def test_history_tool_returns_messages_with_ids() -> None:
    manager = ConversationManager()
    conversation = await _seed(manager)
    capability = HistoryCapability(manager)
    assert capability.toolset is not None
    search_history = capability.toolset.tools["search_history"].function
    ctx = cast(RunContext[Any], SimpleNamespace(conversation_id=str(conversation.id)))

    hits = await search_history(ctx, "bug")
    assert [m.message_text for m in hits] == [
        "find the auth bug",
        "the bug is in login",
    ]
    assert all(m.id is not None for m in hits)
