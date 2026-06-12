"""Live replay against a real Slack workspace (`pytest tests/trigger/test_slack.py`).

Each test replays a canonical scenario script through the real SlackTentacle
into the chat configured under `trigger.slack` in octomate.yaml; pass/fail only
checks that something was posted and no render failed — the human inspects the
rendering in Slack."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.channel.slack import SlackTentacle

from tests.trigger.conftest import TriggerTargets
from tests.support.scenarios import (
    action_batch,
    batch_actions,
    play,
    showcase,
    streamed_text,
)

pytestmark = pytest.mark.trigger


@pytest.fixture
def slack_channel(
    live_config: OctomateConfig,
    trigger_targets: TriggerTargets,
    in_memory_engine: AsyncEngine,
) -> tuple[SlackTentacle, ConversationKey]:
    config = live_config.channels.slack
    if config is None or not config.enabled or trigger_targets.slack is None:
        pytest.skip("slack credentials/trigger target not configured in octomate.yaml")
    if not trigger_targets.slack.thread_id:
        pytest.skip(
            "trigger.slack needs a thread_id — slack streams into a thread "
            "(use an existing conversation's thread_ts)"
        )
    channel = SlackTentacle("slack", Octomate(), config=config)
    key = ConversationKey(
        channel_tentacle_id="slack", **trigger_targets.slack.model_dump()
    )
    return channel, key


async def test_slack_renders_showcase(
    slack_channel: tuple[SlackTentacle, ConversationKey],
    scenario_image: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, key = slack_channel

    with caplog.at_level("WARNING"):
        message_id = await channel.consume(
            key, play(showcase(image_file=scenario_image))
        )

    assert message_id is not None
    # drive_timeline swallows render errors and keeps draining; surface them here.
    assert "timeline render failed" not in caplog.text


async def test_slack_renders_streamed_text(
    slack_channel: tuple[SlackTentacle, ConversationKey],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, key = slack_channel

    with caplog.at_level("WARNING"):
        message_id = await channel.consume(
            key,
            play(streamed_text("Streaming ", "from the ", "octomate test suite.")),
        )

    assert message_id is not None
    assert "timeline render failed" not in caplog.text


async def test_slack_presents_action_batch(
    slack_channel: tuple[SlackTentacle, ConversationKey],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, key = slack_channel
    # The slack buttons serialize the batch id into their state; the scenario
    # actions need real ids (on a live run the action manager sets them).
    batch_id = uuid4()
    question, approval = batch_actions()
    script = action_batch(
        batch_id=str(batch_id),
        questions=[question.model_copy(update={"batch_id": batch_id})],
        approvals=[approval.model_copy(update={"batch_id": batch_id})],
    )

    with caplog.at_level("WARNING"):
        # The batch is never answered: the run simply stays suspended.
        await channel.consume(key, play(script))

    assert "timeline render failed" not in caplog.text
