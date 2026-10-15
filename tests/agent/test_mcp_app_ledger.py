"""App invocations survive independently of tool text and installed providers."""

import json

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.database import async_session
from octomate.managers import ThreadManager, UserManager
from octomate.schemas.events import MessageEvent
from octomate.schemas.mcp import McpAppInvocation, NoAuthMcp
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import (
    ThreadCommand,
    ThreadLedger,
    ThreadMcpAppInvocation,
    ThreadMessage,
)
from octomate.schemas.user import UserProfile
from tests.support.managers import a_thread
from tests.support.users import a_user


@pytest.fixture
async def app_entry(in_memory_engine: AsyncEngine) -> ThreadMcpAppInvocation:
    user = await a_user(profiles={"im": "alice"})
    installation = NoAuthMcp(
        user_id=user.id,
        name="Diagrams",
        namespace="personal/diagrams",
        url="https://mcp.example/mcp",
    )
    async with async_session() as session:
        session.add(installation)
        await session.commit()
    opening = await ThreadManager(users=UserManager()).record_inbound(
        MessageEvent(
            tentacle_id="im",
            chat_type="group",
            chat_id="chat",
            user_id="alice",
            message_id="opening",
            sender=UserProfile(channel_user_id="alice"),
            segments=[TextSegment(data={"text": "Draw a diagram"})],
        )
    )
    return ThreadMcpAppInvocation(
        thread_id=opening.thread_id,
        direction="outbound",
        actor_kind="agent",
        sender_id=opening.sender_id,
        agent_tentacle_id="codex",
        app_invocation=McpAppInvocation.model_validate(
            {
                "installation_id": installation.id,
                "namespace": installation.namespace,
                "provider_name": installation.name,
                "resource_uri": "ui://diagrams/app.html",
                "tool": {
                    "name": "create_diagram",
                    "title": "Create diagram",
                    "input_schema": {"type": "object"},
                    "_meta": {"ui": {"resourceUri": "ui://diagrams/app.html"}},
                },
                "arguments": {"xml": "<diagram>✓</diagram>" * 100, "layout": None},
                "result": {
                    "content": [
                        {"type": "text", "text": "complete output ✓\n" * 100},
                        {"type": "image", "data": "aW1hZ2U=", "mime_type": "image/png"},
                        {
                            "type": "resource",
                            "resource": {
                                "uri": "file:///diagram.xml",
                                "mime_type": "application/xml",
                                "text": "<diagram />",
                            },
                        },
                    ],
                    "structured_content": {"diagram": {"nodes": [1, 2], "ready": True}},
                    "_meta": {"uiOnly": {"state": "retained", "revision": 2}},
                },
            }
        ),
    )


@pytest.mark.parametrize("is_error", [False, True])
async def test_complete_app_invocation_roundtrips(
    app_entry: ThreadMcpAppInvocation, is_error: bool
) -> None:
    app_entry.app_invocation.result.is_error = is_error
    async with async_session() as session:
        session.add(app_entry)
        await session.commit()
    async with async_session() as session:
        stored = await session.one_or_none(
            ThreadMessage, expressions=[ThreadMessage["id"] == app_entry.id]
        )
        assert isinstance(stored, ThreadMcpAppInvocation)
        assert stored.app_invocation == app_entry.app_invocation
        assert stored.app_invocation.result.meta == {
            "uiOnly": {"state": "retained", "revision": 2}
        }
        assert stored.app_invocation.result.is_error is is_error
        assert stored.model_messages == []


async def test_generic_ledger_serialization_exposes_only_the_app_reference(
    app_entry: ThreadMcpAppInvocation,
) -> None:
    async with async_session() as session:
        session.add(app_entry)
        await session.commit()
    thread = await ThreadManager(users=UserManager()).get(app_entry.thread_id)
    assert thread is not None
    stored = thread.messages[-1]
    assert isinstance(stored, ThreadMcpAppInvocation)
    adapter = TypeAdapter(ThreadMessage | ThreadCommand | ThreadMcpAppInvocation)
    public = json.loads(adapter.dump_json(stored))
    assert public["kind"] == "mcp_app"
    assert public["app"] == {
        "id": str(app_entry.id),
        "provider_name": "Diagrams",
        "tool_name": "create_diagram",
    }
    assert "app_invocation" not in public
    assert "uiOnly" not in thread.model_dump_json()
    assert "<diagram>" not in thread.model_dump_json()
    assert thread.model_dump(mode="json")["messages"][-1]["app"] == public["app"]


async def test_uninstall_and_namespace_reuse_do_not_rebind_recorded_apps(
    app_entry: ThreadMcpAppInvocation,
) -> None:
    original_id = app_entry.app_invocation.installation_id
    async with async_session() as session:
        session.add(app_entry)
        installation = await session.get(NoAuthMcp, original_id)
        assert installation is not None
        user_id = installation.user_id
        await session.delete(installation)
        await session.commit()
        replacement = NoAuthMcp(
            user_id=user_id,
            name="Replacement",
            namespace=app_entry.app_invocation.namespace,
            url="https://replacement.example/mcp",
        )
        session.add(replacement)
        await session.commit()
    async with async_session() as session:
        stored = await session.get(ThreadMcpAppInvocation, app_entry.id)
        assert stored is not None
        assert stored.app_invocation == app_entry.app_invocation
        assert stored.app_invocation.installation_id != replacement.id


async def test_distinct_app_entries_follow_ledger_paging_and_fork_cuts(
    app_entry: ThreadMcpAppInvocation,
) -> None:
    second = ThreadMcpAppInvocation(
        thread_id=app_entry.thread_id,
        direction="outbound",
        actor_kind="agent",
        sender_id=app_entry.sender_id,
        agent_tentacle_id="codex",
        app_invocation=app_entry.app_invocation.model_copy(
            update={"arguments": {"xml": "<second />"}}, deep=True
        ),
    )
    fork_id = await a_thread("fork")
    async with async_session() as session:
        session.add(app_entry)
        session.add(second)
        await session.flush()
        session.add(
            ThreadLedger(
                thread_id=fork_id,
                ledger_id=app_entry.thread_id,
                cut_message_id=app_entry.id,
            )
        )
        await session.commit()
    threads = ThreadManager(users=UserManager())
    newest = await threads.messages_page(app_entry.thread_id, limit=1, unbound=True)
    assert [row.id for row in newest.items] == [second.id]
    assert newest.has_more
    previous = await threads.messages_page(
        app_entry.thread_id, newest.next_cursor, limit=1, unbound=True
    )
    assert [row.id for row in previous.items] == [app_entry.id]
    fork = await threads.messages_page(fork_id, limit=10)
    assert [
        row.id for row in fork.items if isinstance(row, ThreadMcpAppInvocation)
    ] == [app_entry.id]
    assert isinstance(newest.items[0], ThreadMcpAppInvocation)
    assert newest.items[0].app_invocation.arguments == {"xml": "<second />"}
    thread = await threads.get(app_entry.thread_id)
    assert thread is not None
    pending = await threads.pending_prompt_messages(thread, second.id, "inkling")
    assert [row.platform_message_id for row in pending] == ["opening"]


def test_app_resource_must_be_an_mcp_ui_resource(
    app_entry: ThreadMcpAppInvocation,
) -> None:
    with pytest.raises(ValidationError, match="resource_uri"):
        McpAppInvocation.model_validate(
            app_entry.app_invocation.model_dump()
            | {"resource_uri": "https://unrelated.example/page"}
        )
