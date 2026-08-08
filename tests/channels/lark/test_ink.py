"""Lark ink send routing and tentacle reply-target tests."""

from __future__ import annotations

import asyncio
import json

from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from lark_oapi.core.http import Transport
from pydantic import SecretStr

from octomate.schemas.conversation import ChannelAddress
from octomate.tentacles.channels.lark import LarkTentacle
from octomate.tentacles.channels.lark.ink import SDK_AEXECUTE, LarkInk, pools
from octomate.tentacles.channels.lark.schema import LarkOutboundMessage
from tests.channels.lark.fakes import FakeLarkInk, lark_channel


async def test_lark_ink_selects_group_and_private_targets() -> None:
    ink = FakeLarkInk()
    message = LarkOutboundMessage(msg_type="interactive", content="{}")

    group_id = await ink.send_message("oc_group", "group", [message])
    private_id = await ink.send_message("ou_user", "private", [message])

    assert group_id == "created-1"
    assert private_id == "created-2"
    assert ink.created == [
        ("oc_group", "chat_id", "interactive", "{}"),
        ("ou_user", "open_id", "interactive", "{}"),
    ]


async def test_lark_ink_replies_to_first_message_unless_threaded() -> None:
    ink = FakeLarkInk()
    messages = [
        LarkOutboundMessage(msg_type="interactive", content="one"),
        LarkOutboundMessage(msg_type="interactive", content="two"),
    ]

    first_id = await ink.send_message("oc_group", "group", messages, "om_parent")

    assert first_id == "reply-1"
    assert ink.replies == [("om_parent", "interactive", "one", False)]
    assert ink.created == [("oc_group", "chat_id", "interactive", "two")]


async def test_lark_ink_replies_to_each_message_when_threaded() -> None:
    ink = FakeLarkInk()
    messages = [
        LarkOutboundMessage(msg_type="interactive", content="one"),
        LarkOutboundMessage(msg_type="interactive", content="two"),
    ]

    first_id = await ink.send_message(
        "oc_group",
        "group",
        messages,
        "om_parent",
        reply_in_thread=True,
    )

    assert first_id == "reply-1"
    assert ink.created == []
    assert ink.replies == [
        ("om_parent", "interactive", "one", True),
        ("om_parent", "interactive", "two", True),
    ]


async def test_lark_tentacle_replies_to_key_thread_id_when_it_is_open_message_id() -> (
    None
):
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="group",
        chat_id="oc_group",
        user_id="ou_user",
        thread_id="om_child_message",
    )

    await channel.feelers.markdown.present(address, "thread reply")

    assert ink.created == []
    assert len(ink.replies) == 1
    message_id, msg_type, content, reply_in_thread = ink.replies[0]
    assert message_id == "om_child_message"
    assert msg_type == "interactive"
    assert "thread reply" in content
    assert reply_in_thread is True


async def test_lark_tentacle_private_thread_uses_reply_in_thread() -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="private",
        chat_id="ou_user",
        user_id="ou_user",
        thread_id="om_private_anchor",
    )

    await channel.feelers.markdown.present(address, "private thread reply")

    assert ink.created == []
    assert len(ink.replies) == 1
    message_id, msg_type, content, reply_in_thread = ink.replies[0]
    assert message_id == "om_private_anchor"
    assert msg_type == "interactive"
    assert "private thread reply" in content
    assert reply_in_thread is True


async def test_lark_tentacle_ignores_thread_id_as_reply_target() -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="group",
        chat_id="oc_group",
        user_id="ou_user",
        thread_id="omt_19766a1bf00edb8e",
    )

    await channel.feelers.markdown.present(address, "new message")

    assert ink.replies == []
    assert ink.created[0][:2] == ("oc_group", "chat_id")


async def test_lark_markdown_present_falls_back_to_raw_text_when_card_fails() -> None:
    ink = FakeLarkInk()
    ink.fail_interactive_send = True
    channel = lark_channel(ink)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="private",
        chat_id="ou_user",
        user_id="ou_user",
    )

    message_id = await channel.feelers.markdown.present(address, "| A | B |")

    assert message_id == "created-1"
    assert len(ink.created) == 1
    receive_id, receive_id_type, msg_type, content = ink.created[0]
    assert (receive_id, receive_id_type, msg_type) == ("ou_user", "open_id", "text")
    text = json.loads(content)["text"]
    assert "couldn't render this as a Lark card" in text
    assert "| A | B |" in text


async def test_lark_tentacle_message_callback_invokes_ingest() -> None:
    channel = object.__new__(LarkTentacle)
    raw = P2ImMessageReceiveV1()
    calls: list[P2ImMessageReceiveV1] = []
    done = asyncio.Event()

    async def ingest(raw: P2ImMessageReceiveV1) -> None:
        calls.append(raw)
        done.set()

    channel.ingest = ingest

    channel.sense(raw)
    await asyncio.wait_for(done.wait(), timeout=1)

    assert calls == [raw]


async def test_each_lark_ink_owns_a_pool_and_routing_survives_a_peer_exit() -> None:
    """Two Lark channels: each entered ink pools its own client, requests route
    by the calling `lark.Client`'s Config, and one channel's exit closes only
    its own pool — the survivor keeps its client and the transport patch."""
    first = LarkInk("app-one", SecretStr("secret-one"))
    second = LarkInk("app-two", SecretStr("secret-two"))

    async with first:
        first_pool = first.http
        assert first_pool is not None
        async with second:
            assert second.http is not None
            assert second.http is not first_pool
            assert pools[first.config] is first_pool
            assert pools[second.config] is second.http
        assert second.http is None
        assert second.config not in pools
        assert pools[first.config] is first_pool
        assert not first_pool.is_closed
        assert Transport.aexecute is not SDK_AEXECUTE

    assert not pools
    assert first_pool.is_closed
    assert Transport.aexecute is SDK_AEXECUTE
