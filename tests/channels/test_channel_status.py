"""Platform status routing, cleanup and the limits of plain-text presentation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Literal
from unittest.mock import AsyncMock

import discord
import pytest
from pydantic_ai.usage import RunUsage

from octomate.config import DiscordStreamConfig
from octomate.events import (
    ActionBatchEvent,
    ReflexAwakeEvent,
    RunActivityChangedEvent,
    RunEndedEvent,
    RunResultEvent,
    RunStartedEvent,
)
from octomate.nerve import ChannelTopic, Impulse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.discord.chromo import DiscordChromo
from octomate.tentacles.discord.feelers.status import DiscordStatusFeeler
from octomate.tentacles.feelers.output import (
    DefaultMarkdownFeeler,
    DefaultSegmentsFeeler,
)
from octomate.tentacles.feelers.status import PlainTextStatusFeeler
from octomate.tentacles.lark.feelers.status import LarkStatusFeeler
from octomate.tentacles.napcat.chromo import NapcatChromo
from octomate.tentacles.napcat.ink import NapcatInk
from tests.channels.discord.test_streaming import (
    RecordingDiscordInk,
    discord_address,
    discord_channel,
)
from tests.channels.lark.fakes import FakeLarkInk, lark_channel
from tests.channels.test_status_feeler import advance_time as advance_time
from tests.support.channels import FakeChannelTentacle
from tests.support.nerve import drawing


@pytest.mark.parametrize("surface", ["discord", "lark", "plain"])
@pytest.mark.parametrize("stream", [False, True])
async def test_status_pipeline_renders_activity_wait_and_terminal_cleanup(
    surface: Literal["discord", "lark", "plain"],
    stream: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discord_ink = RecordingDiscordInk()
    deletion = AsyncMock()
    monkeypatch.setattr(discord_ink, "delete_message", deletion)
    lark_ink = FakeLarkInk()
    address = discord_address()
    if surface == "discord":
        channel = discord_channel(discord_ink, DiscordStreamConfig(enabled=stream))
        channel.config.stream.enabled = stream
        channel.feelers.markdown = DefaultMarkdownFeeler(
            ink=discord_ink, chromo=DiscordChromo()
        )
        channel.feelers.status = DiscordStatusFeeler(discord_ink)
    elif surface == "lark":
        channel = lark_channel(lark_ink)
        channel.config.stream.enabled = stream
        address = ChannelAddress(channel.id, "thread", "oc_chat", "ou_user", "om_root")
    else:
        channel = FakeChannelTentacle()
        channel.config.stream.enabled = stream
        channel.feelers.status = PlainTextStatusFeeler(channel.feelers.segments)
        address = replace(address, channel_tentacle_id=channel.id)
    status = channel.feelers.status
    monkeypatch.setattr(type(status), "update_interval", 0)
    presented: asyncio.Queue[str] = asyncio.Queue()
    original = status.present

    async def observe(target: ChannelAddress, text: str) -> None:
        await original(target, text)
        presented.put_nowait(text)

    monkeypatch.setattr(status, "present", observe)
    events = [
        (ReflexAwakeEvent(), "Preparing…"),
        (RunStartedEvent(address=address), "Working…"),
        (
            RunActivityChangedEvent(activity="thinking", display_text="Pondering"),
            "Pondering",
        ),
        (RunActivityChangedEvent(activity="using_tools"), "Using tools…"),
        (ActionBatchEvent(batch_id="batch", blocks_run=True), "Waiting for input…"),
        (RunActivityChangedEvent(activity="writing"), "Writing the response…"),
    ]
    async with drawing(channel):
        topic = ChannelTopic(channel.id)
        for event, expected in events:
            channel.octomate.nerve.deliver(topic, Impulse(event, address))
            assert await asyncio.wait_for(presented.get(), 1) == expected
        channel.octomate.nerve.deliver(
            topic, Impulse(RunResultEvent(output="answer", usage=RunUsage()), address)
        )
        channel.octomate.nerve.deliver(topic, Impulse(RunEndedEvent(), address))
        assert await asyncio.wait_for(presented.get(), 1) == ""
    assert not status.queues
    if surface == "discord":
        deletion.assert_awaited_once_with("500", "801")
        assert discord_ink.typing_events == [("enter", "500"), ("exit", "500")]
        status_edits = [
            edit.content for edit in discord_ink.edits if edit.message_id == "801"
        ]
        assert status_edits == [expected for _, expected in events[1:]]
        answer = (
            discord_ink.edits[-1].content
            if stream
            else discord_ink.sends[-1].messages[0].content
        )
        assert answer == "answer"
    elif surface == "lark":
        assert lark_ink.replies[0][0] == "om_root"
        assert lark_ink.replies[0][3] is True
        assert lark_ink.deleted == ["reply-1"]
        status_edits = [
            content
            for message_id, content in lark_ink.patched
            if message_id == "reply-1"
        ]
        assert [
            json.loads(content)["elements"][0]["text"]["content"]
            for content in status_edits
        ] == [expected for _, expected in events[1:]]
    else:
        assert isinstance(channel, FakeChannelTentacle)
        bodies = [
            message["text"]
            for _, _, messages, *_ in channel.sent
            for message in messages
        ]
        assert bodies[0] == "Activity update: Preparing…"
        assert bodies[-1] == "answer"
        assert len(bodies) == 7


@pytest.mark.parametrize("chat_type", ["dm", "thread"])
async def test_discord_status_escapes_labels_and_uses_the_same_destination_to_clear(
    chat_type: Literal["dm", "thread"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ink = RecordingDiscordInk()
    deletion = AsyncMock()
    monkeypatch.setattr(ink, "delete_message", deletion)
    feeler = DiscordStatusFeeler(ink)
    address = replace(discord_address(), chat_type=chat_type)
    label = "**Thinking** @everyone <@123>"
    await feeler.present(address, label)
    await feeler.present(address, "Waiting for input…")
    await feeler.present(address, "")
    assert ink.sends[0].messages[0].content == discord.utils.escape_markdown(
        discord.utils.escape_mentions(label)
    )
    destination = "400" if chat_type == "dm" else "500"
    assert ink.edits[0].channel_id == destination
    assert ink.edits[0].mentioned_user_ids == ()
    deletion.assert_awaited_once_with(destination, "801")
    assert not feeler.messages


async def test_lark_status_label_is_plain_text_and_clear_retracts_only_its_card() -> (
    None
):
    ink = FakeLarkInk()
    feeler = LarkStatusFeeler(ink)
    address = ChannelAddress("lark", "dm", "ou_user", "ou_user")
    label = '<at id="all">all</at> **Thinking**'
    await feeler.present(address, label)
    card = json.loads(ink.created[0][3])
    assert card["elements"][0]["text"] == {"tag": "plain_text", "content": label}
    assert ink.created[0][:3] == ("ou_user", "open_id", "interactive")
    await feeler.present(address, "")
    assert ink.deleted == ["created-1"]
    assert not feeler.messages


async def test_plain_status_coalesces_changes_without_repeating_or_claiming_completion(
    advance_time: Callable[[float], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ink = NapcatInk("http://napcat.invalid")
    sends = AsyncMock(return_value="message")
    monkeypatch.setattr(ink, "send_message", sends)
    feeler = PlainTextStatusFeeler(
        DefaultSegmentsFeeler(ink=ink, chromo=NapcatChromo())
    )
    presented: asyncio.Queue[str] = asyncio.Queue()
    original = feeler.present

    async def observe(target: ChannelAddress, text: str) -> None:
        await original(target, text)
        presented.put_nowait(text)

    monkeypatch.setattr(feeler, "present", observe)
    address = ChannelAddress("qq", "group", "123", "456")
    feeler.feed(address, RunActivityChangedEvent(activity="thinking"))
    key = ThreadKey.from_address(address)
    try:
        assert await asyncio.wait_for(presented.get(), 1) == "Thinking…"
        feeler.feed(address, RunActivityChangedEvent(activity="using_tools"))
        feeler.feed(address, RunActivityChangedEvent(activity="writing"))
        await asyncio.sleep(0)
        assert sends.await_count == 1
        advance_time(11)
        assert await asyncio.wait_for(presented.get(), 1) == "Writing the response…"
        assert sends.call_args is not None
        args, kwargs = sends.call_args
        assert args[:2] == ("123", "group")
        assert args[2][0].segments == [
            {"type": "text", "data": {"text": "Activity update: Writing the response…"}}
        ]
        assert kwargs["channel_thread_id"] == "123"
        feeler.feed(address, RunActivityChangedEvent(activity="writing"))
        advance_time(11)
        await asyncio.sleep(0)
        assert sends.await_count == 2
        feeler.feed(address, RunEndedEvent())
        await feeler.close()
        assert sends.await_count == 2
        assert key not in feeler.queues
    finally:
        await feeler.close()
        await ink.close()


@pytest.mark.parametrize("surface", ["discord", "lark"])
async def test_missing_end_clears_card_and_typing_after_inactivity(
    surface: Literal["discord", "lark"],
    advance_time: Callable[[float], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ink = RecordingDiscordInk() if surface == "discord" else FakeLarkInk()
    deletion = AsyncMock()
    monkeypatch.setattr(ink, "delete_message", deletion)
    feeler = (
        DiscordStatusFeeler(ink)
        if isinstance(ink, RecordingDiscordInk)
        else LarkStatusFeeler(ink)
    )
    shown = asyncio.Event()
    original = feeler.present

    async def observe(target: ChannelAddress, text: str) -> None:
        await original(target, text)
        shown.set()

    monkeypatch.setattr(feeler, "present", observe)
    feeler.feed(discord_address(), RunActivityChangedEvent(activity="thinking"))
    try:
        await asyncio.wait_for(shown.wait(), 1)
        advance_time(121)
        async with asyncio.timeout(1):
            await asyncio.gather(*feeler.tasks)
        deletion.assert_awaited_once()
        assert not feeler.messages
        if isinstance(ink, RecordingDiscordInk):
            assert ink.typing_events == [("enter", "500"), ("exit", "500")]
    finally:
        await feeler.close()


async def test_failed_status_edit_does_not_prevent_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ink = RecordingDiscordInk(fail_edits=True)
    deletion = AsyncMock()
    monkeypatch.setattr(ink, "delete_message", deletion)
    feeler = DiscordStatusFeeler(ink)
    await feeler.display(discord_address(), "Thinking…")
    await feeler.display(discord_address(), "Writing…")
    await feeler.display(discord_address(), "")
    deletion.assert_awaited_once_with("500", "801")
    assert not feeler.messages
