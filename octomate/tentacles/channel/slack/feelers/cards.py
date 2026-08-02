from __future__ import annotations

from octomate.tentacles.channel.slack.schema import SlackBlock

APPROVAL_CARD_ICON = "check"
QUESTION_CARD_ICON = "comment"


def card_block(
    *,
    title: str,
    icon: str,
    subtitle: str = "",
    body: str = "",
) -> SlackBlock:
    block: SlackBlock = {
        "type": "card",
        "slack_icon": {"type": "icon", "name": icon},
        "title": {"type": "mrkdwn", "text": title, "verbatim": False},
    }
    if subtitle:
        block["subtitle"] = {"type": "mrkdwn", "text": subtitle, "verbatim": False}
    if body:
        block["body"] = {"type": "mrkdwn", "text": body, "verbatim": False}
    return block
