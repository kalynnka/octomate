"""Live replay against a real Napcat endpoint (`pytest tests/trigger/test_napcat.py`).

Each test replays a canonical scenario script through the real NapcatTentacle
into the chat configured under `trigger.napcat` in trigger.yaml. Tests check that
something was posted and no render failed; a human inspects the rendering in QQ.
Napcat has no streaming transport or threads:
a module fixture posts the run notice once, then each test's completed reply
lands after it. Native segments are also read back from OneBot for verification.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import TypeAdapter
from pydantic_ai import AgentRunResult, AgentRunResultEvent
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config import NapcatChannelConfig, OctomateConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import (
    AtData,
    AtSegment,
    FileData,
    FileSegment,
    ImageData,
    ImageSegment,
    MarkdownSegment,
    MessageSegment,
    ReplySegment,
    TextSegment,
)
from octomate.tentacles.napcat import NapcatTentacle
from octomate.tentacles.napcat.schema import (
    ActionResponse,
    NapcatAtSegment,
    NapcatMessageSegment,
)
from octomate.types.json import JsonObject
from tests.support.channels import drive
from tests.support.scenarios import (
    action_batch,
    batch_actions,
    play,
    segment_result_events,
    showcase,
    streamed_text,
)
from tests.trigger.conftest import TriggerTargets, run_banner

pytestmark = [pytest.mark.trigger, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def napcat_run(
    live_config: OctomateConfig,
    trigger_targets: TriggerTargets,
) -> AsyncIterator[tuple[NapcatTentacle, ChannelAddress]]:
    """One tentacle and one run notice, shared by the whole run (no threads)."""
    config = live_config.tentacles.get("napcat")
    if (
        not isinstance(config, NapcatChannelConfig)
        or not config.enabled
        or trigger_targets.napcat is None
    ):
        pytest.skip(
            "napcat channel/trigger target not configured in tentacles.yaml/trigger.yaml"
        )
    target = trigger_targets.napcat
    channel = NapcatTentacle("napcat", Octomate(config=live_config), config=config)
    address = ChannelAddress(
        channel_tentacle_id="napcat",
        chat_type=target.chat_type,
        chat_id=target.chat_id,
        user_id=target.user_id,
    )
    try:
        await channel.feelers.markdown.present(address, run_banner("replays follow."))
        yield channel, address
    finally:
        await channel.ink.close()


@pytest.fixture
def napcat_channel(
    napcat_run: tuple[NapcatTentacle, ChannelAddress],
    in_memory_engine: AsyncEngine,
) -> tuple[NapcatTentacle, ChannelAddress]:
    return napcat_run


async def test_napcat_renders_streamed_str_output(
    napcat_channel: tuple[NapcatTentacle, ChannelAddress],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, address = napcat_channel

    with caplog.at_level("WARNING"):
        message_id = await drive(
            channel,
            address,
            play(streamed_text("String ", "output from the octomate test suite.")),
        )

    assert message_id is not None
    # drive_timeline swallows render errors and keeps draining; surface them here.
    assert "timeline render failed" not in caplog.text


async def test_napcat_renders_streamed_segments_showcase(
    napcat_channel: tuple[NapcatTentacle, ChannelAddress],
    scenario_image: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, address = napcat_channel

    with caplog.at_level("WARNING"):
        message_id = await drive(
            channel, address, play(showcase(image_file=scenario_image))
        )

    assert message_id is not None
    assert "timeline render failed" not in caplog.text


@pytest.mark.parametrize("streamed", [True, False])
async def test_napcat_renders_native_segments(
    napcat_channel: tuple[NapcatTentacle, ChannelAddress],
    scenario_image: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    streamed: bool,
) -> None:
    channel, address = napcat_channel
    reply_to = await channel.feelers.markdown.present(
        address, "NapCat native segment replay quotes this message."
    )
    assert reply_to is not None
    document = tmp_path / "octomate-live-replay.txt"
    document.write_text("NapCat native file attachment from Octomate.\n")
    segments: list[MessageSegment] = [
        ReplySegment(data={"id": reply_to}),
        AtSegment(data=AtData(user_id=address.user_id)),
        TextSegment(data={"text": "Native segment replay: "}),
        MarkdownSegment(data={"text": "**mention, image, and file**"}),
        ImageSegment(data=ImageData(file=scenario_image)),
        FileSegment(data=FileData(file=str(document), name=document.name)),
    ]
    events = segment_result_events(segments) if streamed else []
    events.append(AgentRunResultEvent(AgentRunResult(segments)))

    with caplog.at_level("WARNING"):
        message_id = await drive(channel, address, play(events))

    assert message_id is not None
    assert "timeline render failed" not in caplog.text
    message = await channel.ink.call_api(
        "/get_msg", {"message_id": message_id}, ActionResponse[JsonObject]
    )
    received = TypeAdapter(list[NapcatMessageSegment]).validate_python(
        message["message"]
    )
    assert any(
        isinstance(segment, ReplySegment) and segment.data["id"] == reply_to
        for segment in received
    )
    if address.chat_type == "group":
        assert any(
            isinstance(segment, NapcatAtSegment)
            and segment.data.user_id == address.user_id
            for segment in received
        )
    else:
        # NapCat filters native mentions from one-to-one DMs.
        assert not any(isinstance(segment, NapcatAtSegment) for segment in received)
    assert {"text", "image", "file"} <= {segment.type for segment in received}
    assert (
        "".join(
            str(segment) for segment in received if isinstance(segment, TextSegment)
        )
        == "Native segment replay: mention, image, and file"
    )


async def test_napcat_renders_action_batch(
    napcat_channel: tuple[NapcatTentacle, ChannelAddress],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, address = napcat_channel
    batch_id = uuid7()
    question, approval = batch_actions()
    script = action_batch(
        batch_id=str(batch_id),
        questions=[question.model_copy(update={"batch_id": batch_id})],
        approvals=[approval.model_copy(update={"batch_id": batch_id})],
    )

    with caplog.at_level("WARNING"):
        await drive(channel, address, play(script))

    assert "timeline render failed" not in caplog.text
