from __future__ import annotations

from ipaddress import IPv4Address
from typing import Annotated, Self

from pydantic import (
    Field,
    IPvAnyAddress,
    SecretStr,
    ValidationError,
    ValidatorFunctionWrapHandler,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from octomate.config.agents import (
    AgentRouteModelName,
    AgentsConfig,
    ClaudeCodeConfig,
    ClaudeSSHConfig,
    CodexConfig,
    DeepseekConfig,
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
from octomate.config.database import DatabaseSettings, database_settings
from octomate.config.integrations import (
    GitHubIntegrationConfig,
    GitHubMcpConfig,
    IntegrationConfig,
    LinearIntegrationConfig,
    LinearMcpConfig,
)
from octomate.config.mcp import McpConfig, McpIntegrationConfig, McpServerConfig
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
from octomate.schemas.project import Project


class OctomateConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OCTOMATE__",
        env_nested_delimiter="__",
        env_file=".env",
        yaml_file=("octomate.default.yaml", "octomate.yaml"),
        yaml_config_section="octomate",
        nested_model_default_partial_update=True,
        hide_input_in_errors=True,
        extra="ignore",
    )

    host: IPvAnyAddress = IPv4Address("127.0.0.1")
    port: Annotated[int, Field(ge=1, le=65535)] = 8000

    hook_secret: Annotated[
        SecretStr | None,
        Field(
            description="Bearer credential the Claude/Codex hook routers require. "
            "Required whenever one of those agents is configured, since serving a hook "
            "router unauthenticated would let anything that can reach the port write a "
            "session's prompts and answers into thread history. Set it in the "
            "environment (OCTOMATE__HOOK_SECRET) and the installed hooks will reference "
            "the same variable."
        ),
    ] = None

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    logfire: LogfireConfig = Field(default_factory=LogfireConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    mcp: dict[str, McpServerConfig] = Field(
        default_factory=dict,
        description=(
            "Bare vendor MCP servers mounted process-wide, each connected with one "
            "operator credential. The key names the server: its MCP session id, and "
            "the prefix its tools are exposed under."
        ),
    )
    integrations: dict[str, IntegrationConfig] = Field(
        default_factory=dict,
        description=(
            "Per-user OAuth integrations, each authorizing its own account from the "
            "channel. The key names the integration: its connector id, the capability "
            "the model loads, and the prefix its tools carry; `type` selects the "
            "provider that builds it, so one vendor can be mounted once per account."
        ),
    )
    oauth: OAuthConfig = Field(default_factory=OAuthConfig)
    users: dict[str, UserConfig] = Field(
        default_factory=dict,
        description=(
            "Registered cross-channel users keyed by stable username; profiles are "
            "reconciled into the registry at startup."
        ),
    )
    projects: dict[str, Project.Create] = Field(
        default_factory=dict,
        description=(
            "Declared code locations keyed by project name, reconciled into the "
            "registry at startup. Declaring one is the operator vouching for its "
            "contents, which reach an agent as instructions. Unrelated to "
            "`~/.claude/projects/`, which is transcript storage."
        ),
    )

    @field_validator("projects", mode="wrap")
    @classmethod
    def report_what_the_projects_block_said(
        cls, value: object, handler: ValidatorFunctionWrapHandler
    ) -> dict[str, Project.Create]:
        """Put the block back into its own error, which `hide_input_in_errors` takes out.

        That setting is here because most of this config is credentials. `projects:` is
        roots and descriptions, so it is the one block that can safely print itself —
        and it is the one that needs to, since the mistake it invites is a list where a
        mapping keyed by name belongs, and that fails as a bare `dict_type` with nothing
        to say what was written.
        """
        try:
            return handler(value)
        except ValidationError as error:
            faults = "; ".join(
                f"{'.'.join(str(part) for part in fault['loc']) or 'the block'}: "
                f"{fault['msg']}"
                for fault in error.errors()
            )
            raise ValueError(f"{faults} — got {value!r}") from error

    @model_validator(mode="after")
    def validate_oauth_configuration(self) -> Self:
        """Every enabled integration stores credentials, so one of them needs the key."""
        enabled = [name for name, it in self.integrations.items() if it.enabled]
        if enabled and self.oauth.encryption_key is None:
            names = ", ".join(f"integrations.{name}" for name in enabled)
            raise ValueError(
                f"oauth.encryption_key is required when {names} is enabled"
            )
        return self

    @model_validator(mode="after")
    def validate_channel_agent_routes(self) -> Self:
        agent_ids = {"inkling"}
        if self.agents.claude is not None:
            agent_ids.add("claude")
        if self.agents.codex is not None and self.agents.codex.enabled:
            agent_ids.add("codex")
        if self.agents.deepseek is not None and self.agents.deepseek.enabled:
            agent_ids.add("deepseek")
        inkling_models = {model.name for model in self.agents.inkling.models}

        errors: list[InitErrorDetails] = []

        channels: tuple[tuple[str, ChannelConfig | None], ...] = (
            ("slack", self.channels.slack),
            ("lark", self.channels.lark),
            ("napcat", self.channels.napcat),
            ("trunkline", self.channels.trunkline),
        )
        for channel_id, channel in channels:
            if channel is None:
                continue
            routes: list[tuple[tuple[str | int, ...], AgentModelConfig]] = [
                (("agents", index), agent_config)
                for index, agent_config in enumerate(channel.agents)
            ]
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
                    if self.agents.claude is None:
                        errors.append(
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "channel_agent_route",
                                    "claude agent is not configured",
                                    {},
                                ),
                                loc=("channels", channel_id, *route_location, "agent"),
                                input=route.agent,
                            )
                        )
                        continue
                    if route.model not in self.agents.claude.models:
                        errors.append(
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "channel_agent_route",
                                    "{model} is not configured in agents.claude.models",
                                    {"model": repr(route.model)},
                                ),
                                loc=("channels", channel_id, *route_location, "model"),
                                input=route.model,
                            )
                        )
                if route.agent == "codex":
                    if self.agents.codex is None or not self.agents.codex.enabled:
                        errors.append(
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "channel_agent_route",
                                    "codex agent is not configured",
                                    {},
                                ),
                                loc=("channels", channel_id, *route_location, "agent"),
                                input=route.agent,
                            )
                        )
                        continue
                    if route.model not in self.agents.codex.models:
                        errors.append(
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "channel_agent_route",
                                    "{model} is not configured in agents.codex.models",
                                    {"model": repr(route.model)},
                                ),
                                loc=("channels", channel_id, *route_location, "model"),
                                input=route.model,
                            )
                        )
                if route.agent == "deepseek":
                    if self.agents.deepseek is None or not self.agents.deepseek.enabled:
                        errors.append(
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "channel_agent_route",
                                    "deepseek agent is not configured",
                                    {},
                                ),
                                loc=("channels", channel_id, *route_location, "agent"),
                                input=route.agent,
                            )
                        )
                        continue
                    if route.model not in self.agents.deepseek.models:
                        errors.append(
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "channel_agent_route",
                                    "{model} is not configured in agents.deepseek.models",
                                    {"model": repr(route.model)},
                                ),
                                loc=("channels", channel_id, *route_location, "model"),
                                input=route.model,
                            )
                        )
                if route.agent == "inkling" and route.model not in inkling_models:
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

    @model_validator(mode="after")
    def validate_user_links(self) -> Self:
        """A typo'd channel id in a user's links must fail the boot, not
        silently produce a link no channel will ever resolve."""
        channel_ids = {
            channel_id
            for channel_id, channel in (
                ("slack", self.channels.slack),
                ("lark", self.channels.lark),
                ("napcat", self.channels.napcat),
                ("trunkline", self.channels.trunkline),
            )
            if channel is not None
        }
        errors: list[InitErrorDetails] = []
        for username, user in self.users.items():
            for channel_id, profile in user.profiles.items():
                if channel_id not in channel_ids:
                    errors.append(
                        InitErrorDetails(
                            type=PydanticCustomError(
                                "user_link_channel",
                                "{channel} does not match a configured channel",
                                {"channel": repr(channel_id)},
                            ),
                            loc=("users", username, "profiles", channel_id),
                            input=profile.channel_user_id,
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
