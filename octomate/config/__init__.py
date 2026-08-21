"""The config package's public surface: `OctomateConfig` and every leaf model it
validates into, re-exported so a caller imports from `octomate.config` and never has
to know which module owns a name. The deployment itself lives in `base.py`."""

from __future__ import annotations

from octomate.config.agents import (
    AgentRouteModelName,
    AgentsConfig,
    ClaudeCodeConfig,
    ClaudeSSHConfig,
    CodexConfig,
    DeepseekConfig,
    InklingConfig,
)
from octomate.config.base import (
    CONFIG_FILES,
    DEFAULTS_DIR,
    OCTOMATE_HOME_ENV,
    OctomateConfig,
    config_files,
    config_home,
)
from octomate.config.channels import (
    AgentModelConfig,
    ChannelConfig,
    ChannelConfigVariant,
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
from octomate.config.oauth import OAuthConfig
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
from octomate.config.workspaces import WorkspacesConfig

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
    "DeepseekConfig",
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
    # config home
    "CONFIG_FILES",
    "DEFAULTS_DIR",
    "OCTOMATE_HOME_ENV",
    "config_files",
    "config_home",
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
    "WorkspacesConfig",
    # channels
    "AgentModelConfig",
    "ChannelConfig",
    "ChannelConfigVariant",
    "ChannelStreamConfig",
    "LarkChannelConfig",
    "LarkStreamConfig",
    "NapcatChannelConfig",
    "NapcatStreamConfig",
    "SlackChannelConfig",
    "SlackStreamConfig",
    # oauth
    "OAuthConfig",
    # observability
    "LogfireConfig",
    "LoggingConfig",
    "LogLevel",
]
