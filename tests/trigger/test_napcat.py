"""Live replay against a real Napcat endpoint (`pytest tests/trigger/test_napcat.py`).

Each test replays a canonical scenario script through the real NapcatTentacle
into the chat configured under `trigger.napcat` in octomate.yaml; pass/fail
only checks that something was posted and no render failed — the human
inspects the rendering in QQ. Napcat has no streaming transport or threads:
a session fixture posts the run notice once, then each test's reply lands as
a plain message after it."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.channel.napcat import NapcatTentacle

from tests.trigger.conftest import TriggerTargets, run_banner
from tests.support.scenarios import (
    action_batch,
    batch_actions,
    plain_answer,
    plain_deferred_requests,
    plain_segments,
    play,
    segments_reply,
    showcase,
    streamed_text,
)

pytestmark = pytest.mark.trigger


@pytest.fixture(scope="session")
def napcat_run(
    live_config: OctomateConfig,
    trigger_targets: TriggerTargets,
) -> tuple[NapcatTentacle, ConversationKey]:
    """One tentacle and one run notice, shared by the whole run (no threads)."""
    config = live_config.channels.napcat
    if config is None or not config.enabled or trigger_targets.napcat is None:
        pytest.skip("napcat credentials/trigger target not configured in octomate.yaml")
    target = trigger_targets.napcat
    channel = NapcatTentacle("napcat", Octomate(), config=config)
    key = ConversationKey(
        channel_tentacle_id="napcat",
        chat_type=target.chat_type,
        chat_id=target.chat_id,
        user_id=target.user_id,
    )
    asyncio.run(
        channel.feelers.markdown.present(key, run_banner("replays follow."))
    )
    return channel, key


@pytest.fixture
def napcat_channel(
    napcat_run: tuple[NapcatTentacle, ConversationKey],
    in_memory_engine: AsyncEngine,
) -> tuple[NapcatTentacle, ConversationKey]:
    return napcat_run


async def test_napcat_renders_normal_str_output(
    napcat_channel: tuple[NapcatTentacle, ConversationKey],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, key = napcat_channel

    with caplog.at_level("WARNING"):
        message_id = await channel.consume(
            key, play(plain_answer("Normal string output from the octomate test suite."))
        )

    assert message_id is not None
    # drive_timeline swallows render errors and keeps draining; surface them here.
    assert "timeline render failed" not in caplog.text


async def test_napcat_renders_normal_segments_output(
    napcat_channel: tuple[NapcatTentacle, ConversationKey],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, key = napcat_channel

    with caplog.at_level("WARNING"):
        message_id = await channel.consume(key, play(plain_segments(image_file=None)))

    assert message_id is not None
    assert "timeline render failed" not in caplog.text


async def test_napcat_renders_normal_deferred_requests(
    napcat_channel: tuple[NapcatTentacle, ConversationKey],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, key = napcat_channel

    with caplog.at_level("WARNING"):
        message_id = await channel.consume(key, play(plain_deferred_requests()))

    assert message_id is not None
    assert "timeline render failed" not in caplog.text


async def test_napcat_renders_streamed_str_output(
    napcat_channel: tuple[NapcatTentacle, ConversationKey],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, key = napcat_channel

    with caplog.at_level("WARNING"):
        message_id = await channel.consume(
            key,
            play(streamed_text("String ", "output from the octomate test suite.")),
        )

    assert message_id is not None
    # drive_timeline swallows render errors and keeps draining; surface them here.
    assert "timeline render failed" not in caplog.text


async def test_napcat_renders_streamed_segments_output(
    napcat_channel: tuple[NapcatTentacle, ConversationKey],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, key = napcat_channel

    with caplog.at_level("WARNING"):
        message_id = await channel.consume(key, play(segments_reply(image_file=None)))

    assert message_id is not None
    assert "timeline render failed" not in caplog.text


async def test_napcat_renders_streamed_deferred_requests(
    napcat_channel: tuple[NapcatTentacle, ConversationKey],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, key = napcat_channel
    batch_id = uuid4()
    question, approval = batch_actions()
    script = action_batch(
        batch_id=str(batch_id),
        questions=[question.model_copy(update={"batch_id": batch_id})],
        approvals=[approval.model_copy(update={"batch_id": batch_id})],
    )

    with caplog.at_level("WARNING"):
        await channel.consume(key, play(script))

    assert "timeline render failed" not in caplog.text


async def test_napcat_renders_showcase(
    napcat_channel: tuple[NapcatTentacle, ConversationKey],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, key = napcat_channel

    with caplog.at_level("WARNING"):
        message_id = await channel.consume(key, play(showcase()))

    assert message_id is not None
    assert "timeline render failed" not in caplog.text
