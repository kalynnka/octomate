"""How a segment reads when something renders it as text — a prompt's transcript,
or a channel with no native form for it."""

from __future__ import annotations

from octomate.schemas.segments import (
    AtData,
    AtSegment,
    CardData,
    CardSegment,
    FileData,
    FileSegment,
    ImageData,
    ImageSegment,
    MarkdownSegment,
    MessageSegment,
    ReplyData,
    ReplySegment,
    TextSegment,
)
from octomate.types.json import JsonObject

# Slack hangs a card's words off `text`, under blocks; Lark hangs them off
# `content`, under a header and elements. Both are here because the rendering has to
# find them without knowing which platform wrote the payload.
SLACK_CARD: JsonObject = {
    "blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": "Deploy?"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "v2.1 to production"}},
        {
            "type": "actions",
            "elements": [{"type": "button", "text": {"text": "Approve"}}],
        },
    ]
}

LARK_CARD: JsonObject = {
    "header": {"title": {"tag": "plain_text", "content": "Deploy?"}},
    "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "v2.1"}}],
}


def test_every_segment_type_says_more_than_its_name() -> None:
    """The base `__str__` prints `[type]` and nothing else. A segment that falls
    back to it is a hole in the transcript, so each one carries its own."""
    segments: list[MessageSegment] = [
        TextSegment(data={"text": "hello"}),
        MarkdownSegment(data={"text": "## hello"}),
        AtSegment(data=AtData(user_id="U1", name="alice")),
        ImageSegment(data=ImageData(file="/tmp/shot.png", name="shot.png")),
        FileSegment(data=FileData(file="/tmp/report.pdf", name="report.pdf")),
        ReplySegment(data=ReplyData(id="m1", content="earlier")),
        CardSegment(data=CardData(payload=SLACK_CARD)),
    ]

    for segment in segments:
        assert str(segment) != f"[{segment.type}]"


def test_a_file_shows_its_name_and_where_it_is() -> None:
    """As an image does. A named file used to hide the path, which is the half a
    reader needs to open it."""
    segment = FileSegment(data=FileData(file="/tmp/x9.pdf", name="report.pdf"))

    assert str(segment) == "[file: report.pdf | /tmp/x9.pdf]"


def test_an_unnamed_file_still_says_where_it_is() -> None:
    segment = FileSegment(data=FileData(file="/tmp/x9.pdf"))

    assert str(segment) == "[file: file | /tmp/x9.pdf]"


def test_a_slack_card_reads_as_the_words_it_shows() -> None:
    segment = CardSegment(data=CardData(payload=SLACK_CARD))

    assert str(segment) == "[card]\nDeploy?\nv2.1 to production\nApprove"


def test_a_lark_card_reads_the_same_way() -> None:
    segment = CardSegment(data=CardData(payload=LARK_CARD))

    assert str(segment) == "[card]\nDeploy?\nv2.1"


def test_a_buttons_callback_is_not_what_the_button_says() -> None:
    """Slack puts the label under `text` and the payload it sends back under
    `value`. The second is an internal id, and a transcript that printed it would be
    quoting the wiring rather than the card."""
    segment = CardSegment(
        data=CardData(
            payload={
                "elements": [
                    {
                        "type": "button",
                        "text": {"text": "Approve"},
                        "value": "approve_deploy_7f3a",
                    }
                ]
            }
        )
    )

    assert str(segment) == "[card]\nApprove"


def test_a_cards_layout_is_not_its_content() -> None:
    """`tag`, `type` and `template` are how a card is drawn, not what it says."""
    segment = CardSegment(
        data=CardData(payload={"tag": "div", "template": "blue", "type": "section"})
    )

    assert str(segment) == "[card]"


def test_a_card_with_nothing_on_it_is_still_marked() -> None:
    segment = CardSegment(data=CardData(payload={}))

    assert str(segment) == "[card]"
