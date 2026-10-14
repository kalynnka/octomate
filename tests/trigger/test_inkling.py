"""Opt-in model checks (`pytest tests/trigger/test_inkling.py`).

Uses the configured DeepSeek provider with fictional QQ identifiers. No channel
tools are mounted, no message is posted, and no database connection is opened.
"""

from __future__ import annotations

import pytest

from octomate import Octomate
from octomate.config import InklingConfig, OctomateConfig
from octomate.providers import ProviderRegistry
from octomate.schemas.segments import AtSegment, ReplySegment
from octomate.tentacles.inkling import InklingTentacle
from octomate.tentacles.inkling.prompts import SYSTEM_PROMPT

pytestmark = pytest.mark.trigger


async def test_inkling_deepseek_returns_segments(live_config: OctomateConfig) -> None:
    config = live_config.tentacles.get("inkling")
    if not isinstance(config, InklingConfig) or not config.enabled:
        pytest.skip("inkling is not enabled in tentacles.yaml")
    model_config = next(
        (model for model in config.models if model.provider == "deepseek"), None
    )
    if model_config is None:
        pytest.skip("inkling has no configured DeepSeek model")
    model = ProviderRegistry(live_config.providers).build_model(model_config)
    inkling = InklingTentacle(
        "inkling",
        Octomate(config=live_config),
        models={model_config.name: model},
        system_prompt=SYSTEM_PROMPT,
    )

    result = await inkling.agent.run(
        "You are replying in a QQ group; your own QQ ID is 9000. "
        "Alice (3003) #msg:1001: @9000 Plan a Beijing trip from October 22 to 25, "
        "with hotel recommendations and an itinerary. "
        "Reply briefly in Chinese, quote #msg:1001, and mention Alice."
    )

    assert isinstance(result.output, list)
    assert result.output
    assert isinstance(result.output[0], ReplySegment)
    assert result.output[0].data["id"] == "1001"
    assert any(
        isinstance(segment, AtSegment) and segment.data.user_id == "3003"
        for segment in result.output
    )
