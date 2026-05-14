from __future__ import annotations

from collections.abc import AsyncIterator

from pydantic import BaseModel
from pydantic_ai import AgentRunResult, AgentRunResultEvent

from octomate.schemas.segments import AtSegment, ImageSegment, TextSegment
from octomate.tentacles.channel.slack import SlackChromo


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


async def test_slack_chromo_renders_final_text_result() -> None:
    chromo = SlackChromo()

    async def events() -> AsyncIterator[AgentRunResultEvent[str]]:
        yield AgentRunResultEvent(AgentRunResult("hello **Slack**"))

    messages = [message async for message in chromo.squirt(events())]

    assert len(messages) == 1
    assert messages[0].text == "hello **Slack**"
    assert messages[0].blocks is not None
    assert messages[0].blocks[0]["text"]["text"] == "hello *Slack*"


async def test_slack_chromo_renders_structured_output_as_json() -> None:
    class Answer(BaseModel):
        ok: bool
        count: int

    chromo = SlackChromo()

    async def events() -> AsyncIterator[AgentRunResultEvent[Answer]]:
        yield AgentRunResultEvent(AgentRunResult(Answer(ok=True, count=2)))

    messages = [message async for message in chromo.squirt(events())]

    assert len(messages) == 1
    assert messages[0].text.startswith("```json")
    assert '"ok": true' in messages[0].text
    assert '"count": 2' in messages[0].text
