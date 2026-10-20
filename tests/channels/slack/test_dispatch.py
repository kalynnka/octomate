"""Slack inbound dispatch: `on_message` must run the turn off the socket
listener. bolt acks the socket envelope only after the listener returns, so
awaiting the full run (which can park on an approval) would blow Slack's ~3s ack
window and trigger event re-delivery (duplicate runs). The listener instead
schedules `ingest` as a tracked task and returns immediately.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from slack_bolt.async_app import AsyncApp, AsyncSay
from slack_bolt.request.async_request import AsyncBoltRequest
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from octomate.config import SlackChannelConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.user import UserProfile
from octomate.tentacles.slack import base as slack_base
from octomate.tentacles.slack.schema import SlackMessageEvent
from tests.channels.slack.fakes import FakeSlackInk, slack_channel
from tests.support.channels import FakeOctomate


@pytest.mark.parametrize("tab", ["home", "messages"])
async def test_app_home_opened_is_acknowledged_without_starting_a_turn(
    tab: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    host = FakeOctomate()
    channel = slack_base.SlackTentacle(
        "slack",
        host,
        config=SlackChannelConfig(
            app_id="A-test",
            bot_token=SecretStr("xoxb-test"),
            app_token=SecretStr("xapp-test"),
            agents=["inkling"],
        ),
    )
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
    ingest = AsyncMock()
    monkeypatch.setattr(channel, "ingest", ingest)
    response = await channel.app.async_dispatch(
        AsyncBoltRequest(
            body={
                "type": "event_callback",
                "team_id": "T1",
                "event": {
                    "type": "app_home_opened",
                    "user": "U1",
                    "channel": "D1",
                    "tab": tab,
                    "event_ts": "1600000000.000001",
                },
            },
            mode="socket_mode",
        )
    )
    await asyncio.sleep(0)
    assert response.status == 200
    assert response.body == ""
    assert "Unhandled request" not in caplog.text
    ingest.assert_not_awaited()
    assert channel.ingest_tasks == set()
    assert host.kicks == []


async def test_enter_connects_socket_mode_without_parking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSocketModeHandler:
        instances: ClassVar[list[FakeSocketModeHandler]] = []

        app: AsyncApp
        app_token: str
        connected: bool
        closed: bool

        def __init__(self, app: AsyncApp, app_token: str) -> None:
            self.app = app
            self.app_token = app_token
            self.connected = False
            self.closed = False
            FakeSocketModeHandler.instances.append(self)

        async def connect_async(self) -> None:
            self.connected = True

        async def start_async(self) -> None:
            raise AssertionError("Slack start_async blocks app channel startup")

        async def close_async(self) -> None:
            self.closed = True

    monkeypatch.setattr(slack_base, "AsyncSocketModeHandler", FakeSocketModeHandler)
    channel = slack_channel(FakeSlackInk())
    channel.app = AsyncApp(token="xoxb-test")
    channel.handler = None

    async with channel:
        [handler] = FakeSocketModeHandler.instances
        assert handler.connected
        assert handler.app_token == "xapp-test"
        assert channel.handler is handler

    assert handler.closed
    assert channel.handler is None


async def test_on_message_does_not_block_on_the_run() -> None:
    channel = slack_channel(FakeSlackInk())
    channel.self_profile = UserProfile(channel_user_id="bot")
    channel.ingest_tasks = set()

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_ingest(event: object) -> None:
        started.set()
        await release.wait()

    channel.ingest = slow_ingest  # type: ignore[method-assign]
    event = cast(SlackMessageEvent, {"user": "U1", "text": "hi"})

    # If on_message awaited ingest, this would hang (release is never set yet).
    await channel.on_message(event, cast(AsyncSay, None))
    assert len(channel.ingest_tasks) == 1

    await asyncio.sleep(0)
    assert started.is_set()  # the run proceeds in its own task

    task = next(iter(channel.ingest_tasks))
    release.set()
    await task
    await asyncio.sleep(0)  # let the done-callback run
    assert channel.ingest_tasks == set()  # done-callback cleaned up the reference


async def test_on_message_ignores_bot_and_subtype_events() -> None:
    channel = slack_channel(FakeSlackInk())
    channel.self_profile = UserProfile(channel_user_id="bot")
    channel.ingest_tasks = set()

    calls: list[object] = []

    async def record_ingest(event: object) -> None:
        calls.append(event)

    channel.ingest = record_ingest  # type: ignore[method-assign]

    await channel.on_message(
        cast(SlackMessageEvent, {"subtype": "bot_message"}), cast(AsyncSay, None)
    )
    await channel.on_message(
        cast(SlackMessageEvent, {"user": "bot"}), cast(AsyncSay, None)
    )
    # The root Slack writes for a new assistant chat: a title, not the user's words.
    await channel.on_message(
        cast(
            SlackMessageEvent,
            {"subtype": "assistant_app_thread", "user": "U1", "channel_type": "im"},
        ),
        cast(AsyncSay, None),
    )
    await asyncio.sleep(0)

    assert calls == []
    assert channel.ingest_tasks == set()


async def test_open_dm_threads_off_the_opener_a_moving_turn_brings() -> None:
    """`chat.startStream` takes a `thread_ts` that is not optional, so the DM root is
    somewhere this bot can post but never stream — a turn moved there would die on
    `invalid_thread_ts`. A Slack thread hangs off a message, and the opener is it."""
    ink = FakeSlackInk()
    channel = slack_channel(ink)

    address = await channel.open_dm("U-alice", "Finish the migration write-up.")

    assert address is not None
    assert address.chat_id == "D-U-alice"
    assert address.channel_thread_id == "fallback-ts"
    assert address.chat_type == "thread"
    # Private, whatever the type says — a thread inside someone's direct messages is
    # readable by one person, and `scheme` reads that rather than the type.
    assert address.shared is False
    assert [
        message.text
        for _chat, _type, messages, _reply, _channel_thread in ink.sent
        for message in messages
    ] == ["Finish the migration write-up."]


async def test_gateway_thread_opens_one_dm_and_posts_one_root_message() -> None:
    ink = FakeSlackInk()
    channel = slack_channel(ink)
    destination = ChannelAddress(
        channel_tentacle_id=channel.id, chat_type="dm", chat_id="", user_id="U-alice"
    )

    address = await channel.start_thread(destination, "Continue here")

    assert ink.opened_dms == ["U-alice"]
    assert address.chat_id == "D-U-alice"
    assert address.chat_type == "thread"
    assert address.channel_thread_id == "fallback-ts"
    [sent] = ink.sent
    chat_id, chat_type, messages, reply_to, thread_id = sent
    assert chat_id == "D-U-alice"
    assert chat_type == "dm"
    assert reply_to is None
    assert thread_id == "D-U-alice"
    assert [message.text for message in messages] == ["Continue here"]


async def test_open_dm_stays_at_the_root_for_a_caller_with_nothing_to_say() -> None:
    """A `send` only delivers a message, which posts fine at the root. Threading it
    would spend a whole conversation on one line and put words in front of it."""
    ink = FakeSlackInk()
    channel = slack_channel(ink)

    address = await channel.open_dm("U-alice")

    assert address is not None
    assert address.chat_id == "D-U-alice"
    assert address.channel_thread_id is None
    assert address.chat_type == "dm"
    assert ink.sent == []


async def test_open_dm_answers_nothing_where_the_platform_refuses() -> None:
    ink = FakeSlackInk(dm_opens=False)
    channel = slack_channel(ink)

    assert await channel.open_dm("U-alice", "anything") is None
    # Nothing posted: there was no conversation to post into.
    assert ink.sent == []
