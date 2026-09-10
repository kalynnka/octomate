import logging

import pytest
from pydantic import SecretStr
from rich.style import Style

from octomate import Octomate
from octomate.config.channels import TrunklineChannelConfig
from octomate.tentacles.base import TentacleLogFormatter
from octomate.tentacles.claude.base import ClaudeCodeTentacle
from octomate.tentacles.codex.base import CodexTentacle
from octomate.tentacles.deepseek.base import DeepseekTentacle
from octomate.tentacles.discord.base import DiscordTentacle
from octomate.tentacles.inkling.base import InklingTentacle
from octomate.tentacles.lark.base import LarkTentacle
from octomate.tentacles.mcp import BareMcpTentacle
from octomate.tentacles.napcat.base import NapcatTentacle
from octomate.tentacles.slack.base import SlackTentacle
from octomate.tentacles.trunkline.base import TrunklineTentacle


def test_tentacles_have_stable_brand_styles() -> None:
    assert (
        ClaudeCodeTentacle.brand_color,
        CodexTentacle.brand_color,
        DeepseekTentacle.brand_color,
        DiscordTentacle.brand_color,
        InklingTentacle.brand_color,
        LarkTentacle.brand_color,
        NapcatTentacle.brand_color,
        SlackTentacle.brand_color,
        TrunklineTentacle.brand_color,
    ) == (
        Style(color="#D97757", bold=True),
        Style(color="#10A37F", bold=True),
        Style(color="#4D6BFE", bold=True),
        Style(color="#5865F2", bold=True),
        Style(color="#C29145", bold=True),
        Style(color="#666D82", bold=True),
        Style(color="#6A828B", bold=True),
        Style(color="#746576", bold=True),
        Style(color="#D4621A", bold=True),
    )


@pytest.mark.parametrize("mcp_first", [True, False])
async def test_mcp_logs_do_not_claim_channel_logs(
    caplog: pytest.LogCaptureFixture, mcp_first: bool
) -> None:
    octomate = Octomate()
    mcp = BareMcpTentacle(
        "linear_streamify",
        octomate,
        url="https://mcp.example/mcp",
        token=SecretStr("test"),
    )
    channel = TrunklineTentacle(
        "trunkline", octomate, config=TrunklineChannelConfig(agents=["claude"])
    )
    for tentacle in (mcp, channel) if mcp_first else (channel, mcp):
        octomate.connect(tentacle)

    with caplog.at_level(logging.INFO, logger="octomate.tentacles.channel"):
        await channel.probe()

    formatter = TentacleLogFormatter(octomate, colorize=False)
    assert (
        "[channel] Channel trunkline: probed as trunkline (Trunkline)"
        in formatter.format(caplog.records[-1])
    )
    assert octomate.log_tag("octomate.tentacles.trunkline.routes") == (
        channel.id,
        channel.log_color,
    )
    assert octomate.log_tag("octomate.tentacles.mcp") == (mcp.id, mcp.log_color)
    assert octomate.log_tag("octomate.tentacles.feelers.output")[0] == "feelers"
