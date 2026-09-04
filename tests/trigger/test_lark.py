"""Live replay against a real Lark app (`pytest tests/trigger/test_lark.py`).

Each test replays a canonical scenario script through the real LarkTentacle
into the chat configured under `trigger.lark` in trigger.yaml; pass/fail only
checks that something was posted and no render failed — the human inspects the
rendering in Lark. Every test of one pytest run replies under the SAME root:
a session fixture posts a fresh, visible run notice and threads the replays
on its message id."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import LarkChannelConfig, OctomateConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.tentacles.lark import LarkTentacle
from tests.support.channels import drive
from tests.support.scenarios import (
    action_batch,
    agent_run,
    batch_actions,
    mid_run_notice,
    play,
    showcase,
    streamed_text,
    subagent_run,
)
from tests.trigger.conftest import TriggerTargets, run_banner

pytestmark = pytest.mark.trigger


@pytest.fixture(scope="session")
def lark_run_thread(
    live_config: OctomateConfig,
    trigger_targets: TriggerTargets,
) -> tuple[LarkTentacle, ChannelAddress]:
    """One tentacle and one run-notice root message, shared by the whole run."""
    config = live_config.channels.get("lark")
    if (
        not isinstance(config, LarkChannelConfig)
        or not config.enabled
        or trigger_targets.lark is None
    ):
        pytest.skip(
            "lark channel/trigger target not configured in channels.yaml/trigger.yaml"
        )
    target = trigger_targets.lark
    channel = LarkTentacle("lark", Octomate(), config=config)
    main_key = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type=target.chat_type,
        chat_id=target.chat_id,
        user_id=target.user_id,
    )
    root_id = asyncio.run(
        channel.feelers.markdown.present(
            main_key,
            run_banner("replays follow in this thread."),
        )
    )
    if root_id is None:
        pytest.fail("could not post the lark run notice (no message id returned)")
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type=target.chat_type,
        chat_id=target.chat_id,
        user_id=target.user_id,
        channel_thread_id=root_id,
    )
    return channel, address


@pytest.fixture
def lark_channel(
    lark_run_thread: tuple[LarkTentacle, ChannelAddress],
    in_memory_engine: AsyncEngine,
) -> tuple[LarkTentacle, ChannelAddress]:
    return lark_run_thread


async def test_lark_renders_streamed_str_output(
    lark_channel: tuple[LarkTentacle, ChannelAddress],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, address = lark_channel

    with caplog.at_level("WARNING"):
        message_id = await drive(
            channel,
            address,
            play(streamed_text("String ", "output from the octomate test suite.")),
        )

    assert message_id is not None
    # drive_timeline swallows render errors and keeps draining; surface them here.
    assert "timeline render failed" not in caplog.text


async def test_lark_renders_streamed_segments_showcase(
    lark_channel: tuple[LarkTentacle, ChannelAddress],
    scenario_image: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, address = lark_channel

    with caplog.at_level("WARNING"):
        message_id = await drive(
            channel, address, play(showcase(image_file=scenario_image))
        )

    assert message_id is not None
    assert "timeline render failed" not in caplog.text


async def test_lark_renders_action_batch(
    lark_channel: tuple[LarkTentacle, ChannelAddress],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, address = lark_channel
    # The lark card buttons serialize the batch id into their state; the
    # scenario actions need real ids (on a live run the action manager sets them).
    batch_id = uuid4()
    question, approval = batch_actions()
    script = action_batch(
        batch_id=str(batch_id),
        questions=[question.model_copy(update={"batch_id": batch_id})],
        approvals=[approval.model_copy(update={"batch_id": batch_id})],
    )

    with caplog.at_level("WARNING"):
        # The batch is never answered: the run simply stays suspended.
        await drive(channel, address, play(script))

    assert "timeline render failed" not in caplog.text


async def test_lark_renders_mid_run_notice(
    lark_channel: tuple[LarkTentacle, ChannelAddress],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The run notices the user mid-way: a fresh timeline opens below the
    notice while the in-flight tool still closes out the previous one."""
    channel, address = lark_channel

    with caplog.at_level("WARNING"):
        message_id = await drive(channel, address, play(mid_run_notice(), delay=0.2))

    assert message_id is not None
    assert "timeline render failed" not in caplog.text


async def test_lark_renders_timeline(
    lark_channel: tuple[LarkTentacle, ChannelAddress],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The full timeline example: thinking + several github_/linear_ tool calls
    each post as their own card and fold into collapsible panels, with the
    agent's one-line notes between them streaming as answer cards."""
    channel, address = lark_channel

    with caplog.at_level("WARNING"):
        message_id = await drive(channel, address, play(agent_run(), delay=0.2))

    assert message_id is not None
    assert "timeline render failed" not in caplog.text


async def test_lark_renders_subagent_timelines(
    lark_channel: tuple[LarkTentacle, ChannelAddress],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two parallel commissions open sibling cards and fold their reports without
    adding commission rows to the parent timeline."""
    channel, address = lark_channel

    with caplog.at_level("WARNING"):
        message_id = await drive(channel, address, play(subagent_run(), delay=0.2))

    assert message_id is not None
    assert "timeline render failed" not in caplog.text
    assert "Subagent timeline" not in caplog.text
