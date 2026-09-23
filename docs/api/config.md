# `octomate.config`

Settings are validated from the config home's YAML files, the environment and
`.env`; see [Configuration](../installation/configuration.md) for how they are
found and layered.

## The deployment

::: octomate.config.base

::: octomate.config.database

## Agents

::: octomate.config.agents.common

::: octomate.config.agents.claude

::: octomate.config.agents.codex

::: octomate.config.agents.deepseek

::: octomate.config.agents.inkling

## Channels

::: octomate.config.channels
    options:
      members:
        - AgentModelConfig
        - ChannelStreamConfig
        - SlackStreamConfig
        - LarkStreamConfig
        - DiscordStreamConfig
        - ChatRecapConfig
        - ChannelConfig
        - SlackOAuthClientConfig
        - SlackChannelConfig
        - LarkChannelConfig
        - DiscordOAuthClientConfig
        - DiscordChannelConfig
        - TrunklineStreamConfig
        - TrunklineChannelConfig

## MCP connectors

::: octomate.config.mcp.base

::: octomate.config.mcp.pool

## Accounts and OAuth

::: octomate.config.auth

::: octomate.config.oauth

## Projects and workspaces

::: octomate.config.projects

::: octomate.config.mirrors

::: octomate.config.workspaces

## Providers and models

::: octomate.config.providers

::: octomate.config.models

## Observability

::: octomate.config.observability
