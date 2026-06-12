"""Live replay against a real Napcat endpoint (`pytest tests/trigger/test_napcat.py`).

Each test replays a canonical scenario script through the real NapcatTentacle
into the chat configured under `trigger.napcat` in octomate.yaml; pass/fail
only checks that something was posted and no render failed — the human
inspects the rendering in QQ. Napcat has no streaming transport: the default
timeline renders the reply as one final message."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.channel.napcat import NapcatTentacle

from tests.trigger.conftest import TriggerTargets
from tests.support.scenarios import plain_answer, play, showcase

pytestmark = pytest.mark.trigger


@pytest.fixture
def napcat_channel(
    live_config: OctomateConfig,
    trigger_targets: TriggerTargets,
    in_memory_engine: AsyncEngine,
) -> tuple[NapcatTentacle, ConversationKey]:
    config = live_config.channels.napcat
    if config is None or not config.enabled or trigger_targets.napcat is None:
        pytest.skip("napcat credentials/trigger target not configured in octomate.yaml")
    channel = NapcatTentacle("napcat", Octomate(), config=config)
    key = ConversationKey(
        channel_tentacle_id="napcat", **trigger_targets.napcat.model_dump()
    )
    return channel, key


async def test_napcat_renders_plain_answer(
    napcat_channel: tuple[NapcatTentacle, ConversationKey],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, key = napcat_channel

    with caplog.at_level("WARNING"):
        message_id = await channel.consume(
            key, play(plain_answer("Hello from the octomate test suite."))
        )

    assert message_id is not None
    # drive_timeline swallows render errors and keeps draining; surface them here.
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
