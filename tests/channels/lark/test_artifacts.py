"""Native artifact cards retain their conversation and separate ledger delivery."""

import json
from unittest.mock import AsyncMock

import pytest
from pydantic import AnyHttpUrl
from pydantic_ai.usage import RunUsage

from octomate.capabilities.harness.events import (
    ArtifactAvailableEvent,
    RunEndedEvent,
    RunResultEvent,
    RunStartedEvent,
)
from octomate.database import async_session
from octomate.nerve import ChannelTopic, Impulse
from octomate.schemas.artifacts import ArtifactOverview, McpAppArtifact
from octomate.schemas.conversation import ChannelAddress, ChatType
from octomate.schemas.segments import MarkdownSegment
from octomate.schemas.thread import ThreadArtifact, ThreadMessage
from octomate.schemas.user import UserProfile
from tests.agent.test_mcp_app_ledger import app_artifact as app_artifact
from tests.channels.lark.fakes import FakeLarkInk, lark_channel
from tests.support.nerve import drawing


@pytest.mark.parametrize("mode", ["outside", "stream", "quiet"])
@pytest.mark.parametrize(
    ("chat_type", "chat_id", "thread_id"),
    [
        ("dm", "ou_user", None),
        ("group", "oc_group", None),
        ("thread", "oc_group", "om_root"),
        ("thread", "ou_user", "om_private_root"),
    ],
)
async def test_artifact_card_stays_on_its_surface(
    app_artifact: McpAppArtifact,
    mode: str,
    chat_type: ChatType,
    chat_id: str,
    thread_id: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    host = channel.octomate
    host.config.artifacts.viewer_origin = AnyHttpUrl("https://console.example")
    channel.config.stream.enabled = mode == "stream"
    upstream = AsyncMock(side_effect=AssertionError("Cards must not load app HTML"))
    monkeypatch.setattr(host.mcp, "app_resource", upstream)
    address = ChannelAddress("lark", chat_type, chat_id, "ou_user", thread_id)
    await host.users.ensure_profile(
        "lark", UserProfile(channel_user_id="ou_user", user_id=app_artifact.owner_id)
    )
    bot = UserProfile(channel_user_id="ou_bot")
    thread = await host.threads.ensure(address)
    entry = await host.threads.record_artifact(
        thread.id, app_artifact, sender=bot, agent_tentacle_id="codex"
    )
    reply = await host.threads.record_outbound(
        address,
        agent_tentacle_id="codex",
        segments=[MarkdownSegment(data={"text": "done"})],
        sender=bot,
    )
    async with drawing(channel):
        if mode != "outside":
            host.nerve.deliver(
                ChannelTopic(channel.id),
                Impulse(RunStartedEvent(address=address), address),
            )
        host.nerve.deliver(
            ChannelTopic(channel.id),
            Impulse(
                ArtifactAvailableEvent(
                    message_id=entry.id, artifact_id=app_artifact.id
                ),
                address,
            ),
        )
        if mode != "outside":
            host.nerve.deliver(
                ChannelTopic(channel.id),
                Impulse(RunResultEvent(output="done", usage=RunUsage()), address),
            )
            host.nerve.deliver(
                ChannelTopic(channel.id),
                Impulse(RunEndedEvent(thread_message_id=reply.id), address),
            )

    if thread_id is not None:
        assert ink.created == []
        root, msg_type, content, in_thread = ink.replies[0]
        assert root == thread_id
        assert in_thread is True
        message_id = "reply-1"
    else:
        assert ink.replies == []
        recipient, recipient_type, msg_type, content = ink.created[0]
        assert recipient == chat_id
        assert recipient_type == ("open_id" if chat_type == "dm" else "chat_id")
        message_id = "created-1"
    assert msg_type == "interactive"
    assert json.loads(content) == {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "Create diagram"},
            "subtitle": {
                "tag": "plain_text",
                "content": "Diagrams · Interactive artifact",
            },
            "template": "blue",
        },
        "body": {
            "elements": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "Open artifact"},
                    "type": "primary",
                    "behaviors": [
                        {
                            "type": "open_url",
                            "default_url": f"https://console.example/artifacts/{app_artifact.id}",
                        }
                    ],
                }
            ]
        },
    }
    async with async_session() as session:
        stored = await session.get(ThreadArtifact, entry.id)
        assert stored is not None
        assert stored.platform_message_id == message_id
        stored_reply = await session.get(ThreadMessage, reply.id)
        assert stored_reply is not None
        expected_reply = None
        if mode == "stream":
            expected_reply = "stream-1"
        elif mode == "quiet":
            expected_reply = "reply-2" if thread_id else "created-2"
        assert stored_reply.platform_message_id == expected_reply
    upstream.assert_not_awaited()


async def test_card_metadata_stays_plain_text(app_artifact: McpAppArtifact) -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    title = '<at id="all"></at> **Diagram**'
    provider = '<a href="https://example.com">Provider</a>'
    await channel.feelers.artifacts.send(
        ChannelAddress("lark", "dm", "ou_user", "ou_user"),
        ArtifactOverview(
            artifact_id=app_artifact.id,
            title=title,
            provider_name=provider,
            viewer_url=AnyHttpUrl(
                f"https://console.example/artifacts/{app_artifact.id}"
            ),
        ),
    )
    card = json.loads(ink.created[0][3])
    assert card["header"]["title"] == {"tag": "plain_text", "content": title}
    assert card["header"]["subtitle"] == {
        "tag": "plain_text",
        "content": f"{provider} · Interactive artifact",
    }
