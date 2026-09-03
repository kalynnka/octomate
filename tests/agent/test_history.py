"""Message-text/role derivation and the history search + pagination capability."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.exceptions import ModelRetry
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
from pydantic_ai.models.test import TestModel
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.capabilities.history import HistoryCapability
from octomate.managers import ConversationManager, ThreadManager, UserManager
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.events import MessageEvent
from octomate.schemas.messages import ModelRequest, ModelResponse
from octomate.schemas.segments import TextSegment
from octomate.schemas.user import UserProfile
from tests.support.managers import a_thread


def _send_args(*texts: str) -> dict[str, Any]:
    return {"segments": [{"type": "text", "data": {"text": t}} for t in texts]}


def _sent_text(*texts: str) -> str:
    """The message_text a text-only `send` yields: its raw args dict,
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
def test_response_send_text(as_json_str: bool) -> None:
    args = _send_args("sent hi")
    payload: Any = json.dumps(args) if as_json_str else args
    message = ModelResponse(
        parts=[ToolCallPart(tool_name="send", args=payload, tool_call_id="c1")]
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
                tool_name="send",
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
                tool_name="send", args=_send_args("update"), tool_call_id="c1"
            ),
            ToolCallPart(tool_name="other", args={"x": 1}, tool_call_id="c2"),
        ]
    )
    # answer (TextPart) + the send; the non-send tool call contributes nothing.
    assert message.message_text == "answer\n\n" + _sent_text("update")


# --- persistence + search/pagination (in-memory database) ---


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


def _key() -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="dev_ui",
        chat_type="dm",
        chat_id="alice",
        user_id="dev",
    )


def _event(message_id: str, user_id: str, text: str) -> MessageEvent:
    return MessageEvent(
        tentacle_id="dev_ui",
        message_id=message_id,
        chat_type="dm",
        chat_id="alice",
        user_id=user_id,
        sender=UserProfile(channel_user_id=user_id, name=user_id.title()),
        segments=[TextSegment(data={"text": text})],
    )


def _ctx(conversation_id: uuid.UUID) -> RunContext[Any]:
    return RunContext(
        deps=None,
        model=TestModel(),
        usage=RunUsage(),
        conversation_id=str(conversation_id),
    )


async def _seed(manager: ConversationManager) -> Conversation:
    """One run: user turn, an assistant turn that thinks then sends, the tool
    return for that send, and a final assistant answer. conversation_id is stamped
    on each message exactly as pydantic-ai does for a real run."""
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id="inkling")
    cid = str(conversation.id)
    run_id = "run-1"
    ts = datetime.now(UTC)
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
                        tool_name="send",
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


async def _profile(thread_manager: ThreadManager, user_id: str) -> UserProfile:
    profile = await thread_manager.users.profile("dev_ui", user_id)
    assert profile is not None
    return profile


async def _bound(thread_manager: ThreadManager, user_id: str) -> HistoryCapability:
    """The history capability as a run answering `user_id` mounts it."""
    return await HistoryCapability(thread_manager).for_profile(
        await _profile(thread_manager, user_id)
    )


async def test_the_thread_tools_read_what_the_user_spoke_in() -> None:
    thread_manager = ThreadManager(users=UserManager())
    first = await thread_manager.record_inbound(
        _event("m1", "alice", "stored while asleep")
    )
    await thread_manager.record_inbound(_event("m2", "bob", "wake now"))
    capability = await _bound(thread_manager, "alice")
    assert capability.toolset is not None
    ctx = _ctx(uuid.uuid4())
    tools = await capability.toolset.get_tools(ctx)

    hits = await capability.toolset.call_tool(
        "search_thread_history",
        {"query": "asleep"},
        ctx,
        tools["search_thread_history"],
    )
    after = await capability.toolset.call_tool(
        "read_thread_history_after",
        {"message_id": str(first.id), "limit": 1},
        ctx,
        tools["read_thread_history_after"],
    )

    assert [message.message_text for message in hits] == ["stored while asleep"]
    assert [message.message_text for message in after] == ["wake now"]


async def test_a_receiver_reads_every_thread_its_user_spoke_in() -> None:
    """No grant: the person a run answers is the scope. Handed alice's work
    anywhere, the receiver searches the chat she spoke in — bob's replies there
    included — while bob's own direct messages, where she never spoke, are not
    hers; read as bob, they are, and so is the chat he replied in."""
    threads = ThreadManager(users=UserManager())
    await threads.record_inbound(_event("m1", "alice", "find the auth bug"))
    await threads.record_inbound(_event("m2", "bob", "the bug is in login"))
    own = await threads.record_inbound(
        MessageEvent(
            tentacle_id="dev_ui",
            message_id="b1",
            chat_type="dm",
            chat_id="bob",
            user_id="bob",
            sender=UserProfile(channel_user_id="bob", name="Bob"),
            segments=[TextSegment(data={"text": "a bug of my own"})],
        )
    )
    ctx = _ctx(uuid.uuid4())

    async def search(capability: HistoryCapability) -> list[str | None]:
        assert capability.toolset is not None
        tools = await capability.toolset.get_tools(ctx)
        hits = await capability.toolset.call_tool(
            "search_thread_history",
            {"query": "bug"},
            ctx,
            tools["search_thread_history"],
        )
        return [message.message_text for message in hits]

    alice = await _bound(threads, "alice")
    assert alice.toolset is not None
    tools = await alice.toolset.get_tools(ctx)
    before = await alice.toolset.call_tool(
        "read_thread_history_before",
        {"message_id": "#msg:m2", "limit": 5},
        ctx,
        tools["read_thread_history_before"],
    )

    assert await search(alice) == ["find the auth bug", "the bug is in login"]
    assert [message.message_text for message in before] == ["find the auth bug"]
    with pytest.raises(ModelRetry, match="no message"):
        await alice.toolset.call_tool(
            "read_thread_history_after",
            {"message_id": str(own.id)},
            ctx,
            tools["read_thread_history_after"],
        )
    assert await search(await _bound(threads, "bob")) == [
        "find the auth bug",
        "the bug is in login",
        "a bug of my own",
    ]


async def test_the_template_serves_no_run_itself() -> None:
    """The mounted capability is bound per run by `for_profile`; the template's
    own tools have nobody to read for and say so rather than reading nothing."""
    capability = HistoryCapability(ThreadManager(users=UserManager()))

    with pytest.raises(RuntimeError, match="mount the copy `for_profile` gives"):
        await capability.search_thread_history("anything")
