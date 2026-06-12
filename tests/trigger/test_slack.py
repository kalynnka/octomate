"""Live replay against a real Slack workspace (`pytest tests/trigger/test_slack.py`).

Each test replays a canonical scenario script through the real SlackTentacle
into the chat configured under `trigger.slack` in octomate.yaml; pass/fail only
checks that something was posted and no render failed — the human inspects the
rendering in Slack. Every test of one pytest run posts into the SAME thread:
a session fixture opens it with a fresh, visible header message in the DM
(slack streaming only works inside a thread, and a new root per run keeps the
replays at the bottom of the chat instead of buried in an old thread)."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.channel.slack import SlackTentacle

from tests.trigger.conftest import TriggerTargets, run_banner
from tests.support.scenarios import (
    action_batch,
    agent_run,
    batch_actions,
    mid_run_notice,
    play,
    showcase,
    streamed_text,
)

pytestmark = pytest.mark.trigger


@pytest.fixture(scope="session")
def slack_run_thread(
    live_config: OctomateConfig,
    trigger_targets: TriggerTargets,
) -> tuple[SlackTentacle, ConversationKey]:
    """One tentacle and one freshly opened thread, shared by the whole run."""
    config = live_config.channels.slack
    if config is None or not config.enabled or trigger_targets.slack is None:
        pytest.skip("slack credentials/trigger target not configured in octomate.yaml")
    target = trigger_targets.slack
    channel = SlackTentacle("slack", Octomate(), config=config)
    main_key = ConversationKey(
        channel_tentacle_id="slack",
        chat_type=target.chat_type,
        chat_id=target.chat_id,
        user_id=target.user_id,
    )
    thread_ts = asyncio.run(
        channel.feelers.markdown.present(
            main_key,
            run_banner("replays follow in this thread."),
        )
    )
    if thread_ts is None:
        pytest.fail("could not open the slack run thread (no message ts returned)")
    key = ConversationKey(
        channel_tentacle_id="slack",
        chat_type=target.chat_type,
        chat_id=target.chat_id,
        user_id=target.user_id,
        thread_id=thread_ts,
    )
    return channel, key


@pytest.fixture
def slack_channel(
    slack_run_thread: tuple[SlackTentacle, ConversationKey],
    in_memory_engine: AsyncEngine,
) -> tuple[SlackTentacle, ConversationKey]:
    return slack_run_thread


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


async def test_slack_renders_agent_run_timeline(
    slack_channel: tuple[SlackTentacle, ConversationKey],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, key = slack_channel

    with caplog.at_level("WARNING"):
        # Paced playback so the timeline visibly streams in the client.
        message_id = await channel.consume(key, play(agent_run(), delay=0.2))

    assert message_id is not None
    assert "timeline render failed" not in caplog.text


async def test_slack_renders_mid_run_notice(
    slack_channel: tuple[SlackTentacle, ConversationKey],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The run notices the user mid-way: a fresh timeline opens below the
    notice while the in-flight tool still closes out the previous one."""
    channel, key = slack_channel

    with caplog.at_level("WARNING"):
        message_id = await channel.consume(key, play(mid_run_notice(), delay=0.2))

    assert message_id is not None
    assert "timeline render failed" not in caplog.text
