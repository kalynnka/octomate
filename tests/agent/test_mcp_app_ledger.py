"""MCP artifacts outlive their installations and the threads referencing them."""

import json

import pytest
from arcanus import Relation
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.database import async_session
from octomate.managers import ThreadManager, UserManager
from octomate.nerve import Nerve
from octomate.schemas.artifacts import Artifact, McpAppArtifact
from octomate.schemas.events import MessageEvent
from octomate.schemas.mcp import McpAppInvocation, NoAuthMcp
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import (
    Thread,
    ThreadArtifact,
    ThreadCommand,
    ThreadLedger,
    ThreadMessage,
)
from octomate.schemas.user import UserProfile
from tests.support.managers import a_thread
from tests.support.users import a_user


@pytest.fixture
async def app_artifact(in_memory_engine: AsyncEngine) -> McpAppArtifact:
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
    return McpAppArtifact(
        owner_id=user.id,
        title="Create diagram",
        invocation=McpAppInvocation.model_validate(
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


@pytest.fixture
async def app_entry(app_artifact: McpAppArtifact) -> ThreadArtifact:
    opening = await ThreadManager(nerve=Nerve(), users=UserManager()).record_inbound(
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
    return ThreadArtifact(
        thread_id=opening.thread_id,
        direction="outbound",
        actor_kind="agent",
        sender_id=opening.sender_id,
        agent_tentacle_id="codex",
        artifact_id=app_artifact.id,
        artifact=Relation(app_artifact),
    )


@pytest.mark.parametrize("is_error", [False, True])
async def test_complete_artifact_roundtrips_without_a_thread(
    app_artifact: McpAppArtifact, is_error: bool
) -> None:
    app_artifact.invocation.result.is_error = is_error
    async with async_session() as session:
        session.add(app_artifact)
        await session.commit()
    async with async_session() as session:
        stored = await session.one_or_none(
            Artifact, expressions=[Artifact["id"] == app_artifact.id]
        )
        assert isinstance(stored, McpAppArtifact)
        assert stored.owner_id == app_artifact.owner_id
        assert stored.title == app_artifact.title
        assert stored.invocation == app_artifact.invocation
        assert stored.invocation.result.is_error is is_error
        assert await session.count(ThreadMessage) == 0


async def test_generic_ledger_serialization_exposes_only_the_artifact_id(
    app_entry: ThreadArtifact,
    app_artifact: McpAppArtifact,
) -> None:
    async with async_session() as session:
        session.add(app_entry)
        await session.commit()
    thread = await ThreadManager(nerve=Nerve(), users=UserManager()).get(
        app_entry.thread_id
    )
    assert thread is not None
    stored = thread.messages[-1]
    assert isinstance(stored, ThreadArtifact)
    adapter = TypeAdapter(ThreadMessage | ThreadCommand | ThreadArtifact)
    public = json.loads(adapter.dump_json(stored))
    assert public["kind"] == "artifact"
    assert public["artifact_id"] == str(app_artifact.id)
    assert public["id"] == str(app_entry.id)
    assert app_entry.id != app_artifact.id
    assert "artifact" not in public
    assert "uiOnly" not in thread.model_dump_json()
    assert "<diagram>" not in thread.model_dump_json()
    assert stored.model_messages == []


async def test_uninstall_and_namespace_reuse_do_not_rebind_artifacts(
    app_entry: ThreadArtifact,
    app_artifact: McpAppArtifact,
) -> None:
    original_id = app_artifact.invocation.installation_id
    async with async_session() as session:
        session.add(app_entry)
        installation = await session.get(NoAuthMcp, original_id)
        assert installation is not None
        await session.delete(installation)
        await session.commit()
        replacement = NoAuthMcp(
            user_id=app_artifact.owner_id,
            name="Replacement",
            namespace=app_artifact.invocation.namespace,
            url="https://replacement.example/mcp",
        )
        session.add(replacement)
        await session.commit()
    async with async_session() as session:
        stored = await session.get(McpAppArtifact, app_artifact.id)
        assert stored is not None
        assert stored.invocation == app_artifact.invocation
        assert stored.invocation.installation_id != replacement.id


async def test_artifact_survives_deleting_its_thread(
    app_entry: ThreadArtifact,
    app_artifact: McpAppArtifact,
) -> None:
    async with async_session() as session:
        session.add(app_entry)
        await session.commit()
        thread = await session.get(Thread, app_entry.thread_id)
        assert thread is not None
        await session.delete(thread)
        await session.commit()
    async with async_session() as session:
        assert await session.get(ThreadArtifact, app_entry.id) is None
        stored = await session.get(McpAppArtifact, app_artifact.id)
        assert stored is not None
        assert stored.invocation == app_artifact.invocation


async def test_artifact_cannot_be_deleted_while_referenced(
    app_entry: ThreadArtifact,
    app_artifact: McpAppArtifact,
) -> None:
    async with async_session() as session:
        session.add(app_entry)
        await session.commit()
    async with async_session() as session:
        stored = await session.get(McpAppArtifact, app_artifact.id)
        assert stored is not None
        await session.delete(stored)
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            await session.commit()


async def test_distinct_artifact_entries_follow_ledger_paging_and_fork_cuts(
    app_entry: ThreadArtifact,
    app_artifact: McpAppArtifact,
) -> None:
    second_artifact = McpAppArtifact(
        owner_id=app_artifact.owner_id,
        title="Second diagram",
        invocation=app_artifact.invocation.model_copy(
            update={"arguments": {"xml": "<second />"}}, deep=True
        ),
    )
    second = ThreadArtifact(
        thread_id=app_entry.thread_id,
        direction="outbound",
        actor_kind="agent",
        sender_id=app_entry.sender_id,
        agent_tentacle_id="codex",
        artifact_id=second_artifact.id,
        artifact=Relation(second_artifact),
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
    threads = ThreadManager(nerve=Nerve(), users=UserManager())
    newest = await threads.messages_page(app_entry.thread_id, limit=1, unbound=True)
    assert [row.id for row in newest.items] == [second.id]
    assert newest.has_more
    previous = await threads.messages_page(
        app_entry.thread_id, newest.next_cursor, limit=1, unbound=True
    )
    assert [row.id for row in previous.items] == [app_entry.id]
    fork = await threads.messages_page(fork_id, limit=10)
    assert [row.id for row in fork.items if isinstance(row, ThreadArtifact)] == [
        app_entry.id
    ]
    assert isinstance(newest.items[0], ThreadArtifact)
    assert newest.items[0].artifact_id == second_artifact.id
    thread = await threads.get(app_entry.thread_id)
    assert thread is not None
    pending = await threads.pending_prompt_messages(thread, second.id, "inkling")
    assert [row.platform_message_id for row in pending] == ["opening"]


def test_app_resource_must_be_an_mcp_ui_resource(app_artifact: McpAppArtifact) -> None:
    with pytest.raises(ValidationError, match="resource_uri"):
        McpAppInvocation.model_validate(
            app_artifact.invocation.model_dump()
            | {"resource_uri": "https://unrelated.example/page"}
        )
