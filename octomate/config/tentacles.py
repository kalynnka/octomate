"""Tentacle declarations keyed by their deployment identity."""

from typing import Annotated

from pydantic import Field

from octomate.config.acp import AcpConfig
from octomate.config.agents import (
    ClaudeCodeConfig,
    CodexConfig,
    DeepseekConfig,
    InklingConfig,
)
from octomate.config.channels import ChannelConfigVariant
from octomate.config.mcp import McpConfigVariant

type TentacleConfigVariant = Annotated[
    AcpConfig
    | ClaudeCodeConfig
    | CodexConfig
    | DeepseekConfig
    | InklingConfig
    | ChannelConfigVariant
    | McpConfigVariant,
    Field(discriminator="type"),
]
