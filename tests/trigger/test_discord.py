"""Live replay against a real Discord bot (`pytest tests/trigger/test_discord.py`).

The test opens one public thread under the text channel configured in
`trigger.discord`, then replays every Discord output surface there. OAuth is the
exception: it deliberately lands in the configured user's DM. Pass/fail checks
message ids, render warnings, and a forced Gateway reconnect; a human inspects the
rendering in Discord.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from octomate import Octomate
from octomate.capabilities.harness.events import OAuthDeviceAuthorizationEvent
from octomate.config import DiscordChannelConfig, OctomateConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.schemas.segments import (
    FileData,
    FileSegment,
    ImageData,
    ImageSegment,
    MarkdownSegment,
)
from octomate.tentacles.discord import DiscordTentacle
from tests.support.channels import drive
from tests.support.scenarios import (
    agent_run,
    mid_run_notice,
    plain_answer,
    play,
    segment_result_events,
    streamed_text,
    subagent_run,
)
from tests.trigger.conftest import TriggerTargets, run_banner

pytestmark = [pytest.mark.trigger, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def discord_run_thread(
    live_config: OctomateConfig,
    trigger_targets: TriggerTargets,
) -> AsyncIterator[tuple[DiscordTentacle, ChannelAddress]]:
    """One real Gateway client and one fresh public thread for the whole replay."""
    config = live_config.channels.get("discord")
    target = trigger_targets.discord
    if (
        not isinstance(config, DiscordChannelConfig)
        or not config.enabled
        or target is None
    ):
        pytest.skip(
            "discord channel/trigger target not configured in "
            "channels.yaml/trigger.yaml"
        )
    if target.chat_type != "group":
        pytest.skip("discord live replay requires a group text-channel target")

    channel = DiscordTentacle("discord", Octomate(), config=config)
    main_address = ChannelAddress(
        channel_tentacle_id="discord",
        chat_type="group",
        chat_id=target.chat_id,
        user_id=target.user_id,
        shared=True,
    )
    async with channel:
        thread_address = await channel.start_sub_thread(
            main_address,
            run_banner("Discord replays follow in this thread."),
        )
        yield channel, thread_address


async def test_discord_renders_final_text(
    discord_run_thread: tuple[DiscordTentacle, ChannelAddress],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, address = discord_run_thread

    with caplog.at_level("WARNING"):
        message_id = await drive(
            channel,
            address,
            play(plain_answer("Final text without streamed deltas.")),
        )

    assert message_id is not None
    assert "timeline render failed" not in caplog.text


async def test_discord_renders_long_streamed_text(
    discord_run_thread: tuple[DiscordTentacle, ChannelAddress],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, address = discord_run_thread
    long_text = "Discord long streamed output — " + "octopus " * 320

    with caplog.at_level("WARNING"):
        message_id = await drive(
            channel,
            address,
            play(
                streamed_text(
                    long_text[:900],
                    long_text[900:1800],
                    long_text[1800:],
                ),
                delay=0.3,
            ),
        )

    assert message_id is not None
    assert "timeline render failed" not in caplog.text


async def test_discord_renders_native_attachments(
    discord_run_thread: tuple[DiscordTentacle, ChannelAddress],
    scenario_image: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, address = discord_run_thread
    document = tmp_path / "octomate-live-replay.txt"
    document.write_text("Discord native file attachment from Octomate.\n")

    with caplog.at_level("WARNING"):
        message_id = await drive(
            channel,
            address,
            play(
                segment_result_events(
                    [
                        MarkdownSegment(
                            data={"text": "## Native attachments\nImage and file."}
                        ),
                        ImageSegment(
                            data=ImageData(
                                file=scenario_image,
                                name="scenario.png",
                                summary="Discord live replay image",
                            )
                        ),
                        FileSegment(
                            data=FileData(
                                file=str(document),
                                name=document.name,
                                size=document.stat().st_size,
                            )
                        ),
                    ]
                )
            ),
        )

    assert message_id is not None
    assert "timeline render failed" not in caplog.text


async def test_discord_renders_mid_run_notice(
    discord_run_thread: tuple[DiscordTentacle, ChannelAddress],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, address = discord_run_thread

    with caplog.at_level("WARNING"):
        message_id = await drive(channel, address, play(mid_run_notice(), delay=0.1))

    assert message_id is not None
    assert "timeline render failed" not in caplog.text


async def test_discord_renders_timeline(
    discord_run_thread: tuple[DiscordTentacle, ChannelAddress],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, address = discord_run_thread

    with caplog.at_level("WARNING"):
        message_id = await drive(channel, address, play(agent_run(), delay=0.05))

    assert message_id is not None
    assert "timeline render failed" not in caplog.text


async def test_discord_renders_subagent_output(
    discord_run_thread: tuple[DiscordTentacle, ChannelAddress],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, address = discord_run_thread

    with caplog.at_level("WARNING"):
        message_id = await drive(channel, address, play(subagent_run(), delay=0.05))

    assert message_id is not None
    assert "timeline render failed" not in caplog.text


async def test_discord_renders_action_controls(
    discord_run_thread: tuple[DiscordTentacle, ChannelAddress],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, address = discord_run_thread
    batch_id = uuid4()
    choice_question = DeferredQuestion(
        batch_id=batch_id,
        tool_name="ask_questions",
        tool_call_id="call_discord_choice",
        args={
            "question": "Which Discord choice should I take?",
            "choices": ["A", "B"],
        },
    )
    text_question = DeferredQuestion(
        batch_id=batch_id,
        tool_name="ask_questions",
        tool_call_id="call_discord_text",
        args={"question": "What should I type into the Discord modal?"},
    )
    approval = DeferredApproval(
        batch_id=batch_id,
        tool_name="deploy",
        tool_call_id="call_discord_approval",
        args=ApprovalRequest(tool_name="deploy"),
    )

    with caplog.at_level("WARNING"):
        question_ids = await channel.feelers.ask_questions.present(
            address,
            [choice_question, text_question],
        )
        approval_ids = await channel.feelers.approvals.present(address, [approval])

    assert set(question_ids) == {choice_question.id, text_question.id}
    assert all(message_id for message_id in question_ids.values())
    assert approval_ids.get(approval.id)
    assert "timeline render failed" not in caplog.text


async def test_discord_renders_oauth_in_dm(
    discord_run_thread: tuple[DiscordTentacle, ChannelAddress],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, address = discord_run_thread

    with caplog.at_level("WARNING"):
        message_id = await channel.feelers.oauth.present(
            address,
            OAuthDeviceAuthorizationEvent(
                connector_id="github",
                label="GitHub live replay",
                authorization_uri="https://github.com/login/device",
                user_code="OCTO-TEST",
            ),
        )

    assert message_id is not None
    assert "timeline render failed" not in caplog.text


async def test_discord_reconnects_gateway(
    discord_run_thread: tuple[DiscordTentacle, ChannelAddress],
) -> None:
    channel, _address = discord_run_thread
    disconnected = asyncio.Event()
    reconnected = asyncio.Event()

    async def on_disconnect() -> None:
        disconnected.set()

    async def on_ready() -> None:
        reconnected.set()

    async def on_resumed() -> None:
        reconnected.set()

    channel.client.event(on_disconnect)
    channel.client.event(on_ready)
    channel.client.event(on_resumed)
    await channel.client.ws.close(code=1000)
    await asyncio.wait_for(disconnected.wait(), timeout=15)
    await asyncio.wait_for(reconnected.wait(), timeout=45)
