"""The OAuth feeler across channels: cards where the platform has them, plain
text where it does not, and the confirm button that finishes the connection."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import cast

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from pydantic import JsonValue, SecretStr

from octomate.base import Octomate
from octomate.capabilities.harness.events import OAuthAuthorizationEvent
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.oauth import OAuthGrant, OAuthPending
from octomate.schemas.user import UserProfile
from octomate.tentacles.channel.feelers.oauth import PlainTextOAuthFeeler
from octomate.tentacles.channel.lark.base import LarkTentacle
from octomate.tentacles.channel.lark.feelers.actions import LarkCardAction
from octomate.tentacles.channel.lark.feelers.oauth import (
    LarkOAuthFeeler,
    authorization_card_data,
)
from octomate.tentacles.channel.lark.ink import LarkInk
from octomate.tentacles.channel.slack.base import SlackTentacle
from octomate.tentacles.channel.slack.feelers.actions import SlackBlockAction
from octomate.tentacles.channel.slack.feelers.oauth import (
    SlackOAuthFeeler,
    authorization_blocks,
)
from octomate.tentacles.channel.slack.ink import SlackInk
from octomate.tentacles.channel.slack.schema import (
    SlackOAuthActionBody,
    SlackOAuthActionValue,
    SlackOutboundMessage,
)
from octomate.types.json import JsonObject
from tests.channels.lark.fakes import FakeLarkCardsInk
from tests.support.channels import RecordingMarkdownFeeler

AUTHORIZATION = OAuthAuthorizationEvent(
    connector_id="github",
    label="GitHub",
    verification_uri="https://github.com/login/device",
    user_code="ABCD-EFGH",
)


def _obj(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def _objs(value: JsonValue) -> list[JsonObject]:
    assert isinstance(value, list)
    objects: list[JsonObject] = []
    for item in value:
        assert isinstance(item, dict)
        objects.append(item)
    return objects


def _text(value: JsonValue) -> str:
    assert isinstance(value, str)
    return value


def _address(channel: str) -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id=channel,
        chat_type="private",
        chat_id="C1",
        user_id="U1",
        thread_id="",
    )


@dataclass
class FakeOAuthManager:
    """Stands in for `OAuthManager`, recording who asked to complete what."""

    result: OAuthGrant | OAuthPending | None = None
    error: ValueError | None = None
    completions: list[tuple[str, str]] = field(default_factory=list)

    async def complete_latest(
        self,
        profile: UserProfile,
        connector_id: str,
    ) -> OAuthGrant | OAuthPending:
        self.completions.append((profile.channel_user_id, connector_id))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


@dataclass
class FakeUsers:
    async def ensure_profile(
        self, channel_tentacle_id: str, observed: UserProfile
    ) -> UserProfile:
        return observed


@dataclass
class PatchRecordingLarkInk(FakeLarkCardsInk):
    patched: list[tuple[str, JsonObject]] = field(default_factory=list)

    async def patch_card(self, message_id: str, content: str) -> bool:
        self.patched.append((message_id, json.loads(content)))
        return True


@dataclass
class UpdateRecordingSlackInk:
    sent: list[tuple[str, str, list[SlackOutboundMessage], str | None]] = field(
        default_factory=list
    )
    updated: list[tuple[str, str, str, list[JsonObject]]] = field(default_factory=list)

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[SlackOutboundMessage],
        thread_ts: str | None = None,
    ) -> str:
        self.sent.append((chat_id, chat_type, messages, thread_ts))
        return f"slack-{len(self.sent)}"

    async def update_message(
        self,
        channel: str,
        ts: str,
        *,
        text: str,
        blocks: list[JsonObject],
    ) -> None:
        self.updated.append((channel, ts, text, blocks))


async def test_plain_text_feeler_sends_the_link_and_code() -> None:
    markdown = RecordingMarkdownFeeler()

    await PlainTextOAuthFeeler(markdown).present(_address("napcat"), AUTHORIZATION)

    [(address, text)] = markdown.calls
    assert address.channel_tentacle_id == "napcat"
    assert "https://github.com/login/device" in text
    assert "ABCD-EFGH" in text
    # No button to press here, so the message has to say what to do instead.
    assert "confirm" in text


async def test_lark_feeler_sends_a_card_carrying_the_authorization() -> None:
    ink = FakeLarkCardsInk()

    message_id = await LarkOAuthFeeler(cast(LarkInk, ink)).present(
        _address("lark"), AUTHORIZATION
    )

    assert message_id == "lark-1"
    [(_chat_id, _chat_type, messages, _reply_to, _in_thread)] = ink.sent
    card = _obj(json.loads(messages[0].content))
    assert (
        _text(_obj(_obj(card["header"])["title"])["content"]) == "GitHub Device OAuth"
    )
    elements = _objs(card["elements"])
    # Lark card markdown has no code span, so a backticked code would show its
    # backticks to the user.
    assert "ABCD-EFGH" in _text(elements[0]["content"])
    assert "`" not in _text(elements[0]["content"])
    open_button, confirm_button = _objs(elements[2]["actions"])
    # Lark reads a link button's target from the element, not from `value`, and
    # rejects the card outright when it is missing.
    assert open_button["url"] == "https://github.com/login/device"
    assert "value" not in open_button
    # The confirm button carries the authorization back, so a press that lands
    # early can redraw this same card rather than replacing it with a dead end.
    assert confirm_button["value"] == {
        "action": LarkCardAction.OAUTH_CONFIRM.value,
        "connector_id": "github",
        "label": "GitHub",
        "verification_uri": "https://github.com/login/device",
        "user_code": "ABCD-EFGH",
    }


def _lark_confirm(channel: LarkTentacle) -> P2CardActionTriggerResponse:
    card = authorization_card_data(AUTHORIZATION)
    confirm_button = _objs(_objs(card["elements"])[2]["actions"])[1]
    return channel.on_card_action(
        cast(
            P2CardActionTrigger,
            SimpleNamespace(
                event=SimpleNamespace(
                    action=SimpleNamespace(
                        value=confirm_button["value"], form_value={}
                    ),
                    operator=SimpleNamespace(open_id="ou_user", user_id=""),
                    context=SimpleNamespace(open_message_id="om_card"),
                )
            ),
        )
    )


def _lark_channel(
    ink: PatchRecordingLarkInk,
    oauth: FakeOAuthManager,
) -> LarkTentacle:
    channel = object.__new__(LarkTentacle)
    channel.id = "lark"
    channel.ink = cast(LarkInk, ink)
    channel.user_profiles = {
        "ou_user": UserProfile(channel_user_id="ou_user", name="Alice")
    }
    channel.octomate = cast(
        Octomate,
        SimpleNamespace(oauth=oauth, users=FakeUsers()),
    )
    return channel


async def test_lark_confirm_button_completes_and_rewrites_the_card() -> None:
    ink = PatchRecordingLarkInk()
    oauth = FakeOAuthManager(
        result=OAuthGrant(
            access_token=SecretStr("gho_x"),
            subject="42",
            account_label="alice-gh",
        )
    )

    response = _lark_confirm(_lark_channel(ink, oauth))
    await asyncio.sleep(0)

    # The press is acknowledged immediately; the provider poll rewrites the card.
    assert response.toast is not None
    assert oauth.completions == [("ou_user", "github")]
    [(message_id, card)] = ink.patched
    assert message_id == "om_card"
    assert _text(_obj(_obj(card["header"])["title"])["content"]) == "GitHub connected"
    assert "alice-gh" in _text(_objs(card["elements"])[0]["content"])


async def test_lark_confirm_before_authorization_keeps_the_buttons() -> None:
    ink = PatchRecordingLarkInk()
    oauth = FakeOAuthManager(result=OAuthPending(retry_after_seconds=5))

    _lark_confirm(_lark_channel(ink, oauth))
    await asyncio.sleep(0)

    [(_message_id, card)] = ink.patched
    # Redrawn as itself plus a note: the user still needs the link and the code.
    assert (
        _text(_obj(_obj(card["header"])["title"])["content"]) == "GitHub Device OAuth"
    )
    elements = _objs(card["elements"])
    assert "ABCD-EFGH" in _text(elements[0]["content"])
    assert "not accepted the code yet" in _text(elements[1]["content"])
    assert len(_objs(elements[3]["actions"])) == 2


async def test_lark_confirm_reports_a_missing_authorization() -> None:
    ink = PatchRecordingLarkInk()
    oauth = FakeOAuthManager(error=ValueError("no pending github authorization"))

    _lark_confirm(_lark_channel(ink, oauth))
    await asyncio.sleep(0)

    [(_message_id, card)] = ink.patched
    assert _obj(card["header"])["template"] == "red"
    assert "no pending github authorization" in _text(
        _objs(card["elements"])[0]["content"]
    )


async def test_slack_feeler_sends_blocks_carrying_the_authorization() -> None:
    ink = UpdateRecordingSlackInk()

    await SlackOAuthFeeler(cast(SlackInk, ink)).present(
        _address("slack"), AUTHORIZATION
    )

    [(_chat_id, _chat_type, messages, _thread)] = ink.sent
    blocks = _objs(cast(JsonValue, messages[0].blocks))
    assert "ABCD-EFGH" in _text(_obj(blocks[1]["text"])["text"])
    open_button, confirm_button = _objs(blocks[2]["elements"])
    assert open_button["url"] == "https://github.com/login/device"
    assert confirm_button["action_id"] == SlackBlockAction.OAUTH_CONFIRM.value
    value = _obj(json.loads(_text(confirm_button["value"])))
    assert value["connector_id"] == "github"


async def test_slack_confirm_button_completes_and_rewrites_the_message() -> None:
    ink = UpdateRecordingSlackInk()
    oauth = FakeOAuthManager(
        result=OAuthGrant(
            access_token=SecretStr("gho_x"),
            subject="42",
            account_label="alice-gh",
        )
    )
    channel = object.__new__(SlackTentacle)
    channel.id = "slack"
    channel.ink = cast(SlackInk, ink)
    channel.user_profiles = {"U1": UserProfile(channel_user_id="U1", name="Alice")}
    channel.octomate = cast(Octomate, SimpleNamespace(oauth=oauth, users=FakeUsers()))
    blocks = authorization_blocks(AUTHORIZATION)
    confirm_button = _objs(blocks[2]["elements"])[1]

    body: SlackOAuthActionBody = {
        # Slack posts the button's value back as the JSON string it was rendered
        # with; the handler's adapter is what parses it.
        "actions": (
            {
                "action_id": SlackBlockAction.OAUTH_CONFIRM.value,
                "value": cast(SlackOAuthActionValue, _text(confirm_button["value"])),
            },
        ),
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": {"ts": "1700000000.1"},
    }

    await channel.on_oauth_action(_noop_ack, body)

    assert oauth.completions == [("U1", "github")]
    [(channel_id, ts, text, updated)] = ink.updated
    assert (channel_id, ts) == ("C1", "1700000000.1")
    assert text == "GitHub connected"
    assert "alice-gh" in _text(_obj(updated[0]["text"])["text"])


async def _noop_ack() -> None:
    return None
