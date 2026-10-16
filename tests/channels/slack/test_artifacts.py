"""Artifact cards leave on the thread and retain their own delivery identity."""

from unittest.mock import AsyncMock

import pytest
from pydantic import AnyHttpUrl, SecretStr
from pydantic_ai.usage import RunUsage
from slack_bolt.request.async_request import AsyncBoltRequest
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from octomate.capabilities.harness.events import (
    ArtifactAvailableEvent,
    RunEndedEvent,
    RunResultEvent,
    RunStartedEvent,
)
from octomate.config import SlackChannelConfig
from octomate.database import async_session
from octomate.nerve import ChannelTopic, Impulse
from octomate.schemas.artifacts import McpAppArtifact, McpAppOverview
from octomate.schemas.segments import MarkdownSegment
from octomate.schemas.thread import ThreadArtifact, ThreadMessage
from octomate.schemas.user import UserProfile
from octomate.tentacles.slack import SlackTentacle
from octomate.tentacles.slack.feelers.actions import SlackBlockAction
from octomate.tentacles.slack.feelers.artifacts import SlackArtifactFeeler
from octomate.tentacles.slack.ink import SlackInk
from tests.agent.test_mcp_app_ledger import app_artifact as app_artifact
from tests.channels.slack.fakes import FakeSlackInk, slack_channel, slack_key
from tests.support.channels import FakeOctomate
from tests.support.nerve import drawing


@pytest.mark.parametrize("mode", ["outside", "stream", "quiet"])
async def test_artifact_card_is_delivered_in_its_thread(
    app_artifact: McpAppArtifact, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ink = FakeSlackInk()
    channel = slack_channel(ink)
    host = channel.octomate
    host.config.artifacts.viewer_origin = AnyHttpUrl("https://console.example")
    channel.config.stream.enabled = mode == "stream"
    send = AsyncMock(side_effect=["artifact-ts", "reply-ts"])
    monkeypatch.setattr(ink, "send_message", send)
    upstream = AsyncMock(side_effect=AssertionError("Cards must not load app HTML"))
    monkeypatch.setattr(host.mcp, "app_resource", upstream)
    address = slack_key()
    await host.users.ensure_profile(
        "slack", UserProfile(channel_user_id="U1", user_id=app_artifact.owner_id)
    )
    bot = UserProfile(channel_user_id="bot")
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

    call = send.await_args_list[0]
    assert call.args[:2] == (address.chat_id, "thread")
    assert call.kwargs["channel_thread_id"] == address.channel_thread_id
    [card] = call.args[2]
    url = f"https://console.example/artifacts/{app_artifact.id}"
    assert card.text == f"Create diagram\nDiagrams · Interactive artifact\n{url}"
    assert card.blocks == [
        {
            "type": "card",
            "slack_icon": {"type": "icon", "name": "sparkle"},
            "title": {"type": "plain_text", "text": "Create diagram"},
            "subtitle": {
                "type": "plain_text",
                "text": "Diagrams · Interactive artifact",
            },
            "actions": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open"},
                    "accessibility_label": "Open artifact in browser",
                    "url": url,
                    "action_id": SlackBlockAction.ARTIFACT_OPEN.value,
                }
            ],
        },
    ]
    async with async_session() as session:
        saved = await session.get(ThreadArtifact, entry.id)
        assert saved is not None
        assert saved.platform_message_id == "artifact-ts"
        saved_reply = await session.get(ThreadMessage, reply.id)
        assert saved_reply is not None
        assert (
            saved_reply.platform_message_id
            == {
                "outside": None,
                "stream": "stream-ts",
                "quiet": "reply-ts",
            }[mode]
        )
    assert ink.opened_dms == []
    upstream.assert_not_awaited()


async def test_card_metadata_is_literal_and_fits_slack_limits(
    app_artifact: McpAppArtifact, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = FakeOctomate()
    ink = SlackInk(SecretStr("test"))
    call = AsyncMock(return_value={"ts": "artifact-ts"})
    monkeypatch.setattr(ink.client, "api_call", call)
    feeler = SlackArtifactFeeler(artifacts=host.artifacts, users=host.users, ink=ink)
    message_id = await feeler.send(
        slack_key(),
        McpAppOverview(
            artifact_id=app_artifact.id,
            title="<!channel> & " + "T" * 200,
            provider_name="<@U1> " + "P" * 4000,
            viewer_url=AnyHttpUrl(
                f"https://console.example/artifacts/{app_artifact.id}"
            ),
        ),
    )
    assert message_id == "artifact-ts"
    call.assert_awaited_once()
    assert call.await_args is not None
    assert call.await_args.args == ("chat.postMessage",)
    payload = call.await_args.kwargs["json"]
    assert "<!channel>" not in payload["text"]
    assert "<@U1>" not in payload["text"]
    assert payload["text"].startswith("&lt;!channel&gt; &amp;")
    [block] = payload["blocks"]
    assert block["type"] == "card"
    for field in ("title", "subtitle"):
        text = block[field]
        assert isinstance(text, dict)
        assert text["type"] == "plain_text"
        assert isinstance(text["text"], str)
        assert len(text["text"]) == 150


async def test_open_artifact_button_is_acknowledged_without_starting_a_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = FakeOctomate()
    channel = SlackTentacle(
        "slack",
        host,
        config=SlackChannelConfig(
            app_id="A-test",
            bot_token=SecretStr("xoxb-test"),
            app_token=SecretStr("xapp-test"),
            agents=["inkling"],
        ),
    )
    assert isinstance(channel.feelers.artifacts, SlackArtifactFeeler)
    monkeypatch.setattr(
        AsyncWebClient,
        "auth_test",
        AsyncMock(
            return_value=AsyncSlackResponse(
                client=channel.app.client,
                http_verb="POST",
                api_url="https://slack.com/api/auth.test",
                req_args={},
                data={"ok": True, "team_id": "T1", "user_id": "U-bot", "bot_id": "B1"},
                headers={},
                status_code=200,
            )
        ),
    )
    response = await channel.app.async_dispatch(
        AsyncBoltRequest(
            body={
                "type": "block_actions",
                "team": {"id": "T1"},
                "user": {"id": "U1"},
                "channel": {"id": "C1"},
                "actions": [
                    {
                        "type": "button",
                        "action_id": SlackBlockAction.ARTIFACT_OPEN.value,
                    }
                ],
            },
            mode="socket_mode",
        )
    )
    assert response.status == 200
    assert response.body == ""
    assert host.kicks == []
