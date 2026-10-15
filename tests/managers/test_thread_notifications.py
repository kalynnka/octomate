from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.messages import PartDeltaEvent, TextPartDelta
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.harness.events import (
    ThreadCommandOutcomeRecordedEvent,
    ThreadHandoffRecordedEvent,
    ThreadMessageRecordedEvent,
    ThreadProjectBoundEvent,
    ThreadTitleChangedEvent,
    wire_event_adapter,
)
from octomate.database import AsyncSession, async_session
from octomate.nerve import ChannelTopic, Impulse, ThreadTopic, UserTopic
from octomate.schemas.commands import CommandInvocation, CommandResult
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import (
    ThreadCommand,
    ThreadKey,
    ThreadLedger,
    ThreadMessage,
)
from octomate.schemas.user import UserProfile
from tests.support.managers import a_project, a_registry
from tests.support.nerve import held
from tests.support.users import a_user

KEY = ThreadKey("im", "thread", "room", "one")


@pytest.fixture
async def app(in_memory_engine: AsyncEngine) -> Octomate:
    return Octomate()


def prompt(account: str, message_id: str, *, key: ThreadKey = KEY) -> MessageEvent:
    return MessageEvent(
        tentacle_id=key.channel_tentacle_id,
        chat_type=key.chat_type,
        chat_id=key.chat_id,
        channel_thread_id=key.channel_thread_id,
        user_id=account,
        message_id=message_id,
        sender=UserProfile(channel_user_id=account),
        segments=[TextSegment(data={"text": message_id})],
    )


@pytest.mark.parametrize(
    "key", [KEY, ThreadKey("im", "group", "room"), ThreadKey("im", "dm", "room")]
)
async def test_first_participation_notifies_each_user_once_and_only_on_user_topics(
    app: Octomate,
    key: ThreadKey,
) -> None:
    alice = await a_user("alice", profiles={"im": "alice"})
    bob = await a_user("bob", profiles={"im": "bob"})
    carol = await a_user("carol", profiles={"im": "carol"})
    await app.users.ensure_profile(
        "im", UserProfile(channel_user_id="alice-alias", user_id=alice.id)
    )
    with (
        app.nerve.subscribe(UserTopic(alice.id)) as first,
        app.nerve.subscribe(UserTopic(bob.id)) as second,
        app.nerve.subscribe(UserTopic(carol.id)) as other,
        app.nerve.subscribe(ChannelTopic("im")) as channel,
        app.nerve.subscribe(ThreadTopic(key)) as thread_stream,
    ):
        thread = await app.threads.ensure(key, title="Empty")
        assert held(first) == held(second) == held(other) == []
        for index, account in enumerate(("alice", "alice-alias", "bob", "visitor")):
            message = await app.threads.record_inbound(
                prompt(account, str(index), key=key)
            )
            stored = await app.threads.get(thread.id, with_messages=False)
            assert stored is not None
            notification = Impulse(
                ThreadMessageRecordedEvent(
                    thread_id=thread.id,
                    message_id=message.id,
                    updated_at=stored.updated_at,
                )
            )
            assert held(first) == [notification]
            assert held(second) == ([notification] if index >= 2 else [])
            assert held(other) == []
        await app.threads.record_inbound(prompt("bob", "2", key=key))
        assert held(first) == held(second) == []
        assert held(channel) == held(thread_stream) == []
        assert [row.id for row in await app.threads.list_threads(user_id=alice.id)] == [
            thread.id
        ]
        assert await app.threads.list_threads(user_id=carol.id) == []
        assert await app.threads.get(thread.id, user_id=alice.id) is not None
        assert await app.threads.get(thread.id, user_id=carol.id) is None
        await app.users.ensure_profile(
            "im", UserProfile(channel_user_id="visitor", user_id=carol.id)
        )
        bob_profile = await app.users.profile("im", "bob")
        assert bob_profile is not None
        await app.users.unlink_profile(bob, bob_profile.id)
        renamed = await app.threads.ensure(key, title="Changed bindings")
        notification = Impulse(
            ThreadTitleChangedEvent(
                thread_id=thread.id,
                title="Changed bindings",
                updated_at=renamed.updated_at,
            )
        )
        assert held(first) == held(other) == [notification]
        assert held(second) == []
        assert await app.threads.get(thread.id, user_id=carol.id) is not None
        assert await app.threads.get(thread.id, user_id=bob.id) is None
        assert await app.threads.list_threads(user_id=bob.id) == []
        assert wire_event_adapter.dump_python(notification.event, mode="json") == {
            "event_kind": "thread_title_changed",
            "thread_id": str(thread.id),
            "title": "Changed bindings",
            "updated_at": renamed.updated_at.isoformat().replace("+00:00", "Z"),
        }


async def test_first_message_publishes_its_committed_title_and_timestamp(
    app: Octomate,
) -> None:
    alice = await a_user("alice", profiles={"im": "alice"})
    with app.nerve.subscribe(UserTopic(alice.id)) as live:
        message = await app.threads.record_inbound(prompt("alice", "Opening title"))
        stored = await app.threads.get(message.thread_id, with_messages=False)
        assert stored is not None
        assert stored.title == "Opening title"
        assert held(live) == [
            Impulse(
                ThreadMessageRecordedEvent(
                    thread_id=stored.id,
                    message_id=message.id,
                    updated_at=stored.updated_at,
                )
            ),
            Impulse(
                ThreadTitleChangedEvent(
                    thread_id=stored.id,
                    title="Opening title",
                    updated_at=stored.updated_at,
                )
            ),
        ]


@pytest.mark.parametrize(
    "change", ["rename", "channel_title", "message", "project", "handoff", "command"]
)
async def test_committed_listing_changes_notify_participants(
    app: Octomate,
    tmp_path: Path,
    change: Literal[
        "rename", "channel_title", "message", "project", "handoff", "command"
    ],
) -> None:
    alice = await a_user("alice", profiles={"im": "alice"})
    message = await app.threads.record_inbound(prompt("alice", "opening"))
    thread = await app.threads.ensure(KEY)
    with app.nerve.subscribe(UserTopic(alice.id)) as live:
        match change:
            case "rename":
                await app.threads.rename(thread, "Renamed")
            case "channel_title":
                await app.threads.ensure(KEY, title="Channel title")
            case "message":
                await app.threads.record_outbound(
                    thread,
                    agent_tentacle_id="agent",
                    segments=[TextSegment(data={"text": "Reply"})],
                    sender=UserProfile(channel_user_id="bot"),
                )
            case "project":
                project = a_project(tmp_path / "project")
                await a_registry(project)
                await app.threads.bind(thread.id, project)
            case "handoff":
                await app.threads.record_handoff(thread, to_agent_tentacle_id="agent")
            case "command":
                conversation = await app.conversations.ensure(
                    thread.id, agent_tentacle_id="agent"
                )
                receipt = ThreadCommand(
                    thread_id=thread.id,
                    sender_id=message.sender_id,
                    direction="inbound",
                    actor_kind="human",
                    platform_message_id="command",
                    conversation_id=conversation.id,
                    invocation=CommandInvocation(command_id="review"),
                )
                await app.threads.store_message(receipt, thread)
                recorded = held(live)
                assert len(recorded) == 1
                assert isinstance(recorded[0].event, ThreadMessageRecordedEvent)
                assert recorded[0].event.message_id == receipt.id
                await app.threads.record_command_outcome(
                    receipt.id, CommandResult(segments=[])
                )
        notifications = held(live)
        assert len(notifications) == 1
        event = notifications[0].event
        stored = await app.threads.get(thread.id, user_id=alice.id)
        assert stored is not None
        match change, event:
            case (("rename" | "channel_title"), ThreadTitleChangedEvent()):
                assert event.title == (
                    "Renamed" if change == "rename" else "Channel title"
                )
                assert event.title == stored.title
            case "message", ThreadMessageRecordedEvent():
                assert event.message_id == stored.messages[-1].id
            case "project", ThreadProjectBoundEvent():
                assert event.project_id == stored.project_id
            case "handoff", ThreadHandoffRecordedEvent():
                assert stored.latest_handoff is not None
                assert event.handoff.model_dump() == stored.latest_handoff.model_dump()
                assert event.handoff.to_agent_tentacle_id == "agent"
            case "command", ThreadCommandOutcomeRecordedEvent():
                assert event.receipt_id == stored.messages[-1].id
                assert event.outcome == CommandResult(segments=[])
            case _:
                pytest.fail(f"Unexpected event for {change}: {event}")
        assert isinstance(
            event,
            ThreadTitleChangedEvent
            | ThreadMessageRecordedEvent
            | ThreadProjectBoundEvent
            | ThreadHandoffRecordedEvent
            | ThreadCommandOutcomeRecordedEvent,
        )
        assert event.thread_id == thread.id
        if not isinstance(event, ThreadHandoffRecordedEvent):
            assert event.updated_at == stored.updated_at
        payload = wire_event_adapter.dump_python(event, mode="json")
        assert payload["event_kind"] == event.event_kind
        assert payload["thread_id"] == str(thread.id)
        await app.threads.ensure(KEY, title=stored.title)
        await app.threads.rename(stored, stored.title)
        assert held(live) == []


@pytest.mark.parametrize("title", ["Fork", None])
async def test_inherited_history_and_unlinked_profiles_do_not_receive_changes(
    app: Octomate,
    title: str | None,
) -> None:
    alice = await a_user("alice", profiles={"im": "alice"})
    bob = await a_user("bob", profiles={"im": "bob"})
    opening = await app.threads.record_inbound(prompt("alice", "opening"))
    source = await app.conversations.ensure(
        opening.thread_id, agent_tentacle_id="agent"
    )
    target = await app.threads.ensure(ThreadKey("im", "thread", "room", "fork"))
    async with async_session() as session:
        session.add(ThreadLedger(thread_id=target.id, ledger_id=opening.thread_id))
        await session.commit()
    sender = await app.users.profile("im", "bob")
    assert sender is not None
    with (
        app.nerve.subscribe(UserTopic(alice.id)) as inherited,
        app.nerve.subscribe(UserTopic(bob.id)) as own,
    ):
        await app.threads.rename(target, "Inherited")
        assert held(inherited) == held(own) == []
        target = await app.threads.record_fork(
            source, target, sender=sender, title=title
        )
        notifications = held(own)
        assert len(notifications) == 2
        assert isinstance(notifications[0].event, ThreadMessageRecordedEvent)
        assert notifications[0].event.thread_id == target.id
        assert notifications[1] == Impulse(
            ThreadTitleChangedEvent(
                thread_id=target.id, title=title, updated_at=target.updated_at
            )
        )
        assert held(inherited) == []
        assert await app.threads.get(target.id, user_id=alice.id) is None
        assert await app.threads.get(target.id, user_id=bob.id) is not None
        await app.users.unlink_profile(bob, sender.id)
        await app.threads.rename(target, "After unlink")
        assert held(inherited) == held(own) == []


async def test_subthreads_do_not_publish_listing_changes(app: Octomate) -> None:
    alice = await a_user("alice", profiles={"im": "alice"})
    opening = await app.threads.record_inbound(prompt("alice", "opening"))
    parent = await app.threads.ensure(ThreadKey("im", "group", "group"))
    with app.nerve.subscribe(UserTopic(alice.id)) as live:
        child = await app.threads.open_sub_thread(parent)
        await app.threads.store_message(
            ThreadMessage(
                thread_id=child.id,
                sender_id=opening.sender_id,
                direction="inbound",
                actor_kind="human",
                message_text="Child prompt",
            ),
            child,
        )
        await app.threads.rename(child, "Child title")
        assert held(live) == []
        assert child.id not in {
            row.id for row in await app.threads.list_threads(user_id=alice.id)
        }


@pytest.mark.parametrize("change", ["rename", "message"])
async def test_failed_commits_publish_nothing(
    app: Octomate, monkeypatch: pytest.MonkeyPatch, change: Literal["rename", "message"]
) -> None:
    alice = await a_user("alice", profiles={"im": "alice"})
    opening = await app.threads.record_inbound(prompt("alice", "opening"))
    thread = await app.threads.ensure(KEY)
    with (
        app.nerve.subscribe(UserTopic(alice.id)) as live,
        monkeypatch.context() as patch,
    ):
        patch.setattr(
            AsyncSession, "commit", AsyncMock(side_effect=RuntimeError("commit failed"))
        )
        operation = (
            app.threads.rename(thread, "Uncommitted")
            if change == "rename"
            else app.threads.store_message(
                ThreadMessage(
                    thread_id=thread.id,
                    sender_id=opening.sender_id,
                    direction="inbound",
                    actor_kind="human",
                ),
                thread,
            )
        )
        with pytest.raises(RuntimeError, match="commit failed"):
            await operation
        assert held(live) == []
    stored = await app.threads.get(thread.id)
    assert stored is not None
    assert stored.title == "opening"
    assert [message.id for message in stored.messages] == [opening.id]


async def test_token_events_do_not_query_visibility(
    app: Octomate, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = AsyncMock(side_effect=AssertionError("token stream queried the database"))
    monkeypatch.setattr(AsyncSession, "scalars", lookup)
    with app.nerve.subscribe(ThreadTopic(KEY)) as live:
        async with app.nerve.publish(KEY.address("alice")) as stream:
            for _ in range(3):
                await stream.send(
                    Impulse(
                        PartDeltaEvent(index=0, delta=TextPartDelta("word")),
                        KEY.address("alice"),
                    )
                )
        assert len(held(live)) == 3
    lookup.assert_not_called()
