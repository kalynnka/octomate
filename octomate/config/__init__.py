from __future__ import annotations

from octomate.config.agents import (
    AgentRouteModelName,
    AgentsConfig,
    ClaudeCodeConfig,
    ClaudeSSHConfig,
    CodexConfig,
    InklingConfig,
)
from octomate.config.base import OctomateConfig
from octomate.config.channels import (
    AgentModelConfig,
    ChannelConfig,
    ChannelsConfig,
    ChannelStreamConfig,
    LarkChannelConfig,
    LarkStreamConfig,
    NapcatChannelConfig,
    NapcatStreamConfig,
    SlackChannelConfig,
    SlackStreamConfig,
)
from octomate.config.database import DatabaseSettings, database_settings
from octomate.config.integrations import (
    GitHubIntegrationConfig,
    GitHubMcpConfig,
    IntegrationConfig,
    LinearIntegrationConfig,
    LinearMcpConfig,
)
from octomate.config.mcp import McpConfig, McpIntegrationConfig, McpServerConfig
from octomate.config.mirrors import GitIdentity, MirrorsConfig
from octomate.config.models import (
    AnthropicModelSettings,
    BedrockModelSettings,
    CacheTTL,
    ModelConfig,
    OpenAIModelSettings,
    supported_providers,
)
from octomate.config.observability import LogfireConfig, LoggingConfig, LogLevel
from octomate.config.providers import (
    AnthropicProviderConfig,
    BedrockProviderConfig,
    DeepSeekProviderConfig,
    GeminiProviderConfig,
    OpenAIProviderConfig,
    ProvidersConfig,
    VertexProviderConfig,
)
from octomate.config.users import UserConfig

# Grouped by subsystem behind the section comments below, which say more than
# alphabetical order would; sorting this would strand each comment on the wrong name.
__all__ = [  # noqa: RUF022
    "OctomateConfig",
    # agents
    "AgentsConfig",
    "AgentRouteModelName",
    "ClaudeCodeConfig",
    "ClaudeSSHConfig",
    "CodexConfig",
    "InklingConfig",
    # models
    "AnthropicModelSettings",
    "BedrockModelSettings",
    "CacheTTL",
    "ModelConfig",
    "OpenAIModelSettings",
    "supported_providers",
    # providers
    "AnthropicProviderConfig",
    "BedrockProviderConfig",
    "DeepSeekProviderConfig",
    "GeminiProviderConfig",
    "OpenAIProviderConfig",
    "ProvidersConfig",
    "VertexProviderConfig",
    # database
    "DatabaseSettings",
    "database_settings",
    # mcp
    "McpConfig",
    "McpIntegrationConfig",
    "McpServerConfig",
    # integrations
    "IntegrationConfig",
    "GitHubIntegrationConfig",
    "GitHubMcpConfig",
    "LinearIntegrationConfig",
    "LinearMcpConfig",
    # users
    "UserConfig",
    # mirrors
    "GitIdentity",
    "MirrorsConfig",
    # channels
    "AgentModelConfig",
    "ChannelConfig",
    "ChannelsConfig",
    "ChannelStreamConfig",
    "LarkChannelConfig",
    "LarkStreamConfig",
    "NapcatChannelConfig",
    "NapcatStreamConfig",
    "SlackChannelConfig",
    "SlackStreamConfig",
    # observability
    "LogfireConfig",
    "LoggingConfig",
    "LogLevel",
]
