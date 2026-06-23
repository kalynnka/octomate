from __future__ import annotations

from ipaddress import IPv4Address
from typing import Annotated, Self

from pydantic import Field, IPvAnyAddress, ValidationError, model_validator
from pydantic_core import InitErrorDetails, PydanticCustomError
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from octomate.config.agents import (
    AgentsConfig,
    ClaudeCodeConfig,
    ClaudeSSHConfig,
    InklingConfig,
)
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
from octomate.config.mcp import GitHubMcpConfig, LinearMcpConfig, McpConfig
from octomate.config.models import (
    AnthropicModelSettings,
    BedrockModelSettings,
    CacheTTL,
    ModelConfig,
    OpenAIModelSettings,
    ProviderName,
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


class OctomateConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OCTOMATE__",
        env_nested_delimiter="__",
        env_file=".env",
        yaml_file=("octomate.default.yaml", "octomate.yaml"),
        yaml_config_section="octomate",
        nested_model_default_partial_update=True,
        extra="ignore",
    )

    host: IPvAnyAddress = IPv4Address("127.0.0.1")
    port: Annotated[int, Field(ge=1, le=65535)] = 8000

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    logfire: LogfireConfig = Field(default_factory=LogfireConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)

    @model_validator(mode="after")
    def validate_channel_agent_routes(self) -> Self:
        agent_ids = {"inkling"}
        if self.agents.claude is not None:
            agent_ids.add("claude")
        inkling_models = {model.name for model in self.agents.inkling.models}
        errors: list[InitErrorDetails] = []

        channels: tuple[tuple[str, ChannelConfig | None], ...] = (
            ("slack", self.channels.slack),
            ("lark", self.channels.lark),
            ("napcat", self.channels.napcat),
            ("dev_ui", self.channels.dev_ui),
        )
        for channel_id, channel in channels:
            if channel is None:
                continue
            routes: list[tuple[tuple[str | int, ...], AgentModelConfig]] = [
                (("triage",), channel.triage)
            ]
            routes.extend(
                (("receptions", index), reception)
                for index, reception in enumerate(channel.receptions)
            )
            for route_location, route in routes:
                if route.agent not in agent_ids:
                    errors.append(
                        InitErrorDetails(
                            type=PydanticCustomError(
                                "channel_agent_route",
                                "{agent} does not match a configured agent tentacle",
                                {"agent": repr(route.agent)},
                            ),
                            loc=("channels", channel_id, *route_location, "agent"),
                            input=route.agent,
                        )
                    )
                    continue
                if route.agent == "claude":
                    claude_model = (
                        self.agents.claude.model
                        if self.agents.claude is not None
                        else None
                    )
                    if route.model is None:
                        errors.append(
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "channel_agent_route",
                                    "model is required for claude routes",
                                    {},
                                ),
                                loc=("channels", channel_id, *route_location, "model"),
                                input=route.model,
                            )
                        )
                    elif route.model != claude_model:
                        errors.append(
                            InitErrorDetails(
                            type=PydanticCustomError(
                                "channel_agent_route",
                                "{model} is not configured in agents.claude.model",
                                {"model": repr(route.model)},
                                ),
                                loc=("channels", channel_id, *route_location, "model"),
                                input=route.model,
                            )
                        )
                if (
                    route.agent == "inkling"
                    and route.model is not None
                    and route.model not in inkling_models
                ):
                    errors.append(
                        InitErrorDetails(
                            type=PydanticCustomError(
                                "channel_agent_route",
                                "{model} is not configured in agents.inkling.models",
                                {"model": repr(route.model)},
                            ),
                            loc=("channels", channel_id, *route_location, "model"),
                            input=route.model,
                        )
                    )
        if errors:
            raise ValidationError.from_exception_data(
                self.__class__.__name__,
                errors,
            )
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        yaml_settings = YamlConfigSettingsSource(settings_cls)
        return (
            init_settings,
            env_settings,
            yaml_settings,
            dotenv_settings,
            file_secret_settings,
        )


__all__ = [
    "OctomateConfig",
    # agents
    "AgentsConfig",
    "ClaudeCodeConfig",
    "ClaudeSSHConfig",
    "InklingConfig",
    # models
    "AnthropicModelSettings",
    "BedrockModelSettings",
    "CacheTTL",
    "ModelConfig",
    "OpenAIModelSettings",
    "ProviderName",
    # providers
    "AnthropicProviderConfig",
    "BedrockProviderConfig",
    "DeepSeekProviderConfig",
    "GeminiProviderConfig",
    "OpenAIProviderConfig",
    "ProvidersConfig",
    "VertexProviderConfig",
    # mcp
    "McpConfig",
    "GitHubMcpConfig",
    "LinearMcpConfig",
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
