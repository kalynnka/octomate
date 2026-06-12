"""Lark ink send routing and tentacle reply-target tests."""

from __future__ import annotations

import asyncio

from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1

from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.channel.lark import LarkTentacle
from octomate.tentacles.channel.lark.schema import LarkOutboundMessage
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
    key = ConversationKey(
        channel_tentacle_id="lark",
        chat_type="group",
        chat_id="oc_group",
        user_id="ou_user",
        thread_id="om_child_message",
    )

    await channel.feelers.markdown.present(key, "thread reply")

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
    key = ConversationKey(
        channel_tentacle_id="lark",
        chat_type="private",
        chat_id="ou_user",
        user_id="ou_user",
        thread_id="om_private_anchor",
    )

    await channel.feelers.markdown.present(key, "private thread reply")

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
    key = ConversationKey(
        channel_tentacle_id="lark",
        chat_type="group",
        chat_id="oc_group",
        user_id="ou_user",
        thread_id="omt_19766a1bf00edb8e",
    )

    await channel.feelers.markdown.present(key, "new message")

    assert ink.replies == []
    assert ink.created[0][:2] == ("oc_group", "chat_id")


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
