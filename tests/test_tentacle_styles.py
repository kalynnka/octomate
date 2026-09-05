from rich.style import Style

from octomate.tentacles.claude.base import ClaudeCodeTentacle
from octomate.tentacles.codex.base import CodexTentacle
from octomate.tentacles.deepseek.base import DeepseekTentacle
from octomate.tentacles.discord.base import DiscordTentacle
from octomate.tentacles.inkling.base import InklingTentacle
from octomate.tentacles.lark.base import LarkTentacle
from octomate.tentacles.napcat.base import NapcatTentacle
from octomate.tentacles.slack.base import SlackTentacle
from octomate.tentacles.trunkline.base import TrunklineTentacle
from octomate.tentacles.vercel.base import VercelTentacle


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
        VercelTentacle.brand_color,
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
        Style(color="bright_white", bold=True),
    )
