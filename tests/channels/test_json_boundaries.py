"""Channel payload validation preserves the fields providers and callers own."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from octomate.tentacles.lark.feelers import cards
from octomate.tentacles.lark.schema import LarkInteractiveCard, LarkRawCard
from octomate.tentacles.slack.chromo import SlackChromo
from octomate.tentacles.slack.schema import slack_message_adapter


async def test_slack_snapshot_preserves_unknown_nested_fields() -> None:
    document = {
        "type": "message",
        "user": "U1",
        "channel": "C1",
        "ts": "1.2",
        "text": "你好",
        "future": None,
        "blocks": [{"type": "future", "value": [1, None]}],
        "files": [
            {
                "mimetype": "image/png",
                "url_private": "https://example.com/image",
                "name": "image.png",
                "preview": {"future": None},
            }
        ],
    }
    raw = slack_message_adapter.validate_python(document)
    event = await SlackChromo().sip(raw)

    assert event is not None
    assert json.loads(event.raw) == document


def test_lark_nested_card_roundtrip_keeps_callback_values_and_display_options() -> None:
    payload = cards.card_v2(
        [
            cards.collapsible_panel("标题", [cards.markdown("你好")]),
            cards.action(
                [cards.button("Approve", value={"action": "approve", "future": None})]
            ),
        ],
        header=cards.header("Review", template="blue"),
    )
    card = LarkInteractiveCard.model_validate(payload)

    assert (
        json.loads(card.model_dump_json(by_alias=True, exclude_unset=True)) == payload
    )


def test_lark_rejects_malformed_owned_elements_but_carries_external_cards() -> None:
    payload = {"schema": "2.0", "body": {"elements": [{"tag": "markdown"}]}}
    with pytest.raises(ValidationError, match="content"):
        LarkInteractiveCard.model_validate(payload)
    assert json.loads(LarkRawCard(payload).model_dump_json()) == payload
