"""Tentacle declarations keyed by their deployment identity."""

from typing import Annotated

from pydantic import Field

from octomate.config.agents import (
    ClaudeCodeConfig,
    CodexConfig,
    DeepseekConfig,
    InklingConfig,
    ZcodeConfig,
)
from octomate.config.channels import ChannelConfigVariant
from octomate.config.mcp import McpConfigVariant

type TentacleConfigVariant = Annotated[
    ClaudeCodeConfig
    | CodexConfig
    | DeepseekConfig
    | InklingConfig
    | ZcodeConfig
    | ChannelConfigVariant
    | McpConfigVariant,
    Field(discriminator="type"),
]
