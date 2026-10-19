"""Artifact overviews stay in their conversation and keep their own ledger ids."""

from unittest.mock import AsyncMock

import pytest
from arcanus import Relation
from pydantic import AnyHttpUrl
from pydantic_ai.messages import PartStartEvent, TextPart
from pydantic_ai.usage import RunUsage

from octomate.capabilities.harness.events import (
    ArtifactAvailableEvent,
    RunEndedEvent,
    RunResultEvent,
    RunStartedEvent,
)
from octomate.database import async_session
from octomate.managers.artifacts import ArtifactUnavailable
from octomate.nerve import ChannelTopic, Impulse
from octomate.schemas.artifacts import McpAppArtifact
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MarkdownSegment
from octomate.schemas.thread import ThreadArtifact
from octomate.schemas.user import UserProfile
from tests.agent.test_mcp_app_ledger import app_artifact as app_artifact
from tests.agent.test_mcp_app_ledger import app_entry as app_entry
from tests.support.channels import FakeChannelTentacle
from tests.support.nerve import drawing
from tests.support.users import a_user

ADDRESS = ChannelAddress("im", "group", "chat", "alice", shared=True)


@pytest.fixture
def channel() -> FakeChannelTentacle:
    channel = FakeChannelTentacle()
    channel.octomate.config.artifacts.viewer_origin = AnyHttpUrl(
        "https://console.example"
    )
    return channel


@pytest.fixture
async def saved_entry(app_entry: ThreadArtifact) -> ThreadArtifact:
    async with async_session() as session:
        session.add(app_entry)
        await session.commit()
    return app_entry


@pytest.mark.parametrize("stream", [False, True])
async def test_two_artifacts_and_the_reply_have_distinct_deliveries(
    channel: FakeChannelTentacle,
    saved_entry: ThreadArtifact,
    app_artifact: McpAppArtifact,
    stream: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel.config.stream.enabled = stream
    upstream = AsyncMock(side_effect=AssertionError("An overview must not fetch HTML"))
    monkeypatch.setattr(channel.octomate.mcp, "app_resource", upstream)
    second_artifact = McpAppArtifact(
        owner_id=app_artifact.owner_id,
        title="Second diagram",
        source=app_artifact.source,
        invocation=app_artifact.invocation,
    )
    second = ThreadArtifact(
        thread_id=saved_entry.thread_id,
        direction="outbound",
        actor_kind="agent",
        sender_id=saved_entry.sender_id,
        agent_tentacle_id="codex",
        artifact_id=second_artifact.id,
        artifact=Relation(second_artifact),
    )
    async with async_session() as session:
        session.add(second)
        await session.commit()
    reply = await channel.octomate.threads.record_outbound(
        ADDRESS,
        agent_tentacle_id="codex",
        segments=[MarkdownSegment(data={"text": "done"})],
        sender=channel.self_profile,
    )
    async with drawing(channel):
        for event in [
            RunStartedEvent(address=ADDRESS),
            ArtifactAvailableEvent(
                message_id=saved_entry.id, artifact_id=app_artifact.id
            ),
            ArtifactAvailableEvent(
                message_id=second.id, artifact_id=second_artifact.id
            ),
            RunResultEvent(output="done", usage=RunUsage()),
            RunEndedEvent(thread_message_id=reply.id),
        ]:
            channel.octomate.nerve.deliver(
                ChannelTopic(channel.id), Impulse(event, ADDRESS)
            )

    texts = [
        message["text"] for _, _, messages, *_ in channel.sent for message in messages
    ]
    assert texts == [
        f"Create diagram\nDiagrams · Interactive artifact\nhttps://console.example/artifacts/{app_artifact.id}",
        f"Second diagram\nDiagrams · Interactive artifact\nhttps://console.example/artifacts/{second_artifact.id}",
        "done",
    ]
    assert channel.opened_dms == []
    assert all(chat == "chat" and kind == "group" for chat, kind, *_ in channel.sent)
    async with async_session() as session:
        for row, message_id in [
            (saved_entry, "sent-1"),
            (second, "sent-2"),
            (reply, "sent-3"),
        ]:
            stored = await session.get(type(row), row.id)
            assert stored is not None
            assert stored.platform_message_id == message_id
    upstream.assert_not_awaited()


async def test_artifact_outside_a_run_preserves_the_platform_thread(
    channel: FakeChannelTentacle, saved_entry: ThreadArtifact
) -> None:
    address = ChannelAddress(
        "im", "thread", "chat", "alice", "platform-root", shared=True
    )
    async with drawing(channel):
        channel.octomate.nerve.deliver(
            ChannelTopic(channel.id),
            Impulse(
                ArtifactAvailableEvent(
                    message_id=saved_entry.id, artifact_id=saved_entry.artifact_id
                ),
                address,
            ),
        )
    [(chat, kind, _, _, _, thread)] = channel.sent
    assert (chat, kind, thread) == ("chat", "thread", "platform-root")
    assert channel.opened_dms == []
    async with async_session() as session:
        stored = await session.get(ThreadArtifact, saved_entry.id)
        assert stored is not None
        assert stored.platform_message_id == "sent-1"


async def test_artifact_separates_buffered_text_from_following_text(
    channel: FakeChannelTentacle, saved_entry: ThreadArtifact
) -> None:
    channel.config.stream.enabled = True
    async with drawing(channel):
        for event in [
            RunStartedEvent(address=ADDRESS),
            PartStartEvent(index=0, part=TextPart("before")),
            ArtifactAvailableEvent(
                message_id=saved_entry.id, artifact_id=saved_entry.artifact_id
            ),
            PartStartEvent(index=1, part=TextPart("after")),
            RunEndedEvent(),
        ]:
            channel.octomate.nerve.deliver(
                ChannelTopic(channel.id), Impulse(event, ADDRESS)
            )
    texts = [
        message["text"] for _, _, messages, *_ in channel.sent for message in messages
    ]
    assert len(texts) == 3
    assert texts[0] == "before"
    assert str(saved_entry.artifact_id) in texts[1]
    assert texts[2] == "after"


@pytest.mark.parametrize("identity", ["missing", "visitor", "other-owner"])
async def test_artifact_overview_requires_the_addressed_owner(
    channel: FakeChannelTentacle, saved_entry: ThreadArtifact, identity: str
) -> None:
    if identity == "visitor":
        await channel.octomate.users.ensure_profile(
            "im", UserProfile(channel_user_id="bob")
        )
    elif identity == "other-owner":
        await a_user("bob", profiles={"im": "bob"})
    with pytest.raises(ArtifactUnavailable):
        await channel.feelers.artifacts.present(
            ChannelAddress("im", "group", "chat", "bob", shared=True),
            ArtifactAvailableEvent(
                message_id=saved_entry.id, artifact_id=saved_entry.artifact_id
            ),
        )
    assert channel.sent == []
    async with async_session() as session:
        stored = await session.get(ThreadArtifact, saved_entry.id)
        assert stored is not None
        assert stored.platform_message_id is None


@pytest.mark.parametrize("stream", [False, True])
async def test_unconfigured_overviews_leave_the_normal_reply_intact(
    channel: FakeChannelTentacle, saved_entry: ThreadArtifact, stream: bool
) -> None:
    channel.octomate.config.artifacts.viewer_origin = None
    channel.config.stream.enabled = stream
    async with drawing(channel):
        for event in [
            RunStartedEvent(address=ADDRESS),
            ArtifactAvailableEvent(
                message_id=saved_entry.id, artifact_id=saved_entry.artifact_id
            ),
            RunResultEvent(output="done", usage=RunUsage()),
            RunEndedEvent(),
        ]:
            channel.octomate.nerve.deliver(
                ChannelTopic(channel.id), Impulse(event, ADDRESS)
            )
    assert [
        message["text"] for _, _, messages, *_ in channel.sent for message in messages
    ] == ["done"]
    async with async_session() as session:
        stored = await session.get(ThreadArtifact, saved_entry.id)
        assert stored is not None
        assert stored.platform_message_id is None
