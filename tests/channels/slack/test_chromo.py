"""SlackChromo: inbound event decoding and outbound markdown rendering."""

from __future__ import annotations

import pytest

from octomate.schemas.segments import AtSegment, ImageSegment, TextSegment
from octomate.tentacles.channels.slack import SlackChromo
from octomate.types.conversations import ChatType


async def test_slack_chromo_decodes_mentions_and_images() -> None:
    chromo = SlackChromo()
    event = await chromo.sip(
        {
            "ts": "1710000000.000100",
            "user": "U1",
            "channel": "C1",
            "channel_type": "channel",
            "text": "hi <@U2>",
            "files": [
                {
                    "mimetype": "image/png",
                    "url_private": "https://files/image.png",
                    "name": "image.png",
                }
            ],
        }
    )

    assert event is not None
    assert event.chat_type == "group"
    assert [type(seg) for seg in event.segments] == [
        TextSegment,
        AtSegment,
        ImageSegment,
    ]


@pytest.mark.parametrize(
    ("channel_type", "thread_ts", "chat_type", "shared"),
    [
        ("im", "", "dm", False),
        # The assistant pane: a DM whose every message is a thread reply.
        ("im", "1710000000.000001", "thread", False),
        ("app_home", "", "dm", False),
        ("channel", "", "group", True),
        ("channel", "1710000000.000001", "thread", True),
        ("mpim", "", "group", True),
    ],
)
async def test_slack_chromo_reads_the_surface_a_thread_sits_in(
    channel_type: str,
    thread_ts: str,
    chat_type: ChatType,
    shared: bool,
) -> None:
    chromo = SlackChromo()
    event = await chromo.sip(
        {
            "ts": "1710000000.000100",
            "user": "U1",
            "channel": "C1",
            "channel_type": channel_type,
            "thread_ts": thread_ts,
            "text": "hi",
        }
    )

    assert event is not None
    assert event.chat_type == chat_type
    assert event.shared is shared


def test_slack_chromo_renders_markdown_result() -> None:
    chromo = SlackChromo()
    markdown = (
        "# Hello Slack\n\n"
        "Keep **bold**, [links](https://example.com), and tables intact.\n\n"
        "| a | b |\n| - | - |\n| 1 | 2 |"
    )

    messages = chromo.outbound_markdown(markdown)

    assert len(messages) == 1
    assert messages[0].text == markdown
    assert messages[0].markdown_text == markdown
    assert messages[0].blocks is None
