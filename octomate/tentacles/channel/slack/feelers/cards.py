from __future__ import annotations

from typing import Any


APPROVAL_CARD_ICON = "check"
QUESTION_CARD_ICON = "comment"


def card_block(
    *,
    title: str,
    icon: str,
    subtitle: str = "",
    body: str = "",
) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "card",
        "slack_icon": {"type": "icon", "name": icon},
        "title": {"type": "mrkdwn", "text": title, "verbatim": False},
    }
    if subtitle:
        block["subtitle"] = {"type": "mrkdwn", "text": subtitle, "verbatim": False}
    if body:
        block["body"] = {"type": "mrkdwn", "text": body, "verbatim": False}
    return block
