"""The deployment: what a config home is, and the settings class it validates into.

A deployment is a *config home* — one directory holding one flat file per subsystem,
with all tentacle declarations in `tentacles.yaml`. Each file's
top-level keys are `OctomateConfig` field names, which is what lets several files
add up to one settings payload with no wrapper key and no section to traverse.

The home is chosen, never merged. `$OCTOMATE_HOME` wins outright and is obeyed even
when empty — that is what makes the test suite's isolation total. Absent it, the
project's own `./.octomate/config/` is preferred over the machine's
`~/.octomate/config/`, but only if it actually holds config. The `config/`
subdirectory is what marks the server's files as such: `.octomate/` itself belongs
to the database and the client's `cli.toml`, which are not deployment config and
must not make a directory look like one.

The packaged defaults under `defaults/` are the floor beneath whichever home wins.
They are layered per top-level key and wholesale — a home that declares `tentacles:`
replaces the default `tentacles:` entirely rather than merging into it, which is the
behaviour `octomate.default.yaml` and `octomate.yaml` had between them.
"""

from __future__ import annotations

from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated, Self

from octomate_protocol.config import CONFIG_FILES, config_home
from octomate_protocol.config import OCTOMATE_HOME_ENV as OCTOMATE_HOME_ENV
from pydantic import (
    Field,
    IPvAnyAddress,
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

from octomate.config.agents import AgentConfig
from octomate.config.auth import AuthConfig
from octomate.config.channels import ChannelConfig, SlackChannelConfig
from octomate.config.mcp import OAuthMcpConfig
from octomate.config.mcp.base import AuthorizationCodeFlowConfig
from octomate.config.mcp.pool import McpPoolConfig
from octomate.config.mirrors import MirrorsConfig
from octomate.config.oauth import OAuthConfig
from octomate.config.observability import LogfireConfig, LoggingConfig
from octomate.config.projects import ProjectsConfig
from octomate.config.providers import ProvidersConfig
from octomate.config.tentacles import TentacleConfigVariant
from octomate.config.workspaces import WorkspacesConfig
from octomate.schemas.project import Project

DEFAULTS_DIR = Path(__file__).parent / "defaults"


def config_files() -> tuple[Path, ...]:
    """Every YAML a settings class should read, weakest first.

    Absent files are passed through rather than filtered: pydantic-settings skips a
    path that is not a file, and listing them all keeps the returned tuple a
    description of the search rather than of this machine.
    """
    home = config_home()
    return tuple(DEFAULTS_DIR / name for name in CONFIG_FILES) + tuple(
        home / name for name in CONFIG_FILES
    )


class OctomateConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OCTOMATE__",
        env_nested_delimiter="__",
        env_file=".env",
        nested_model_default_partial_update=True,
        hide_input_in_errors=True,
        extra="ignore",
    )

    host: IPvAnyAddress = IPv4Address("127.0.0.1")
    port: Annotated[int, Field(ge=1, le=65535)] = 8000

    tentacles: dict[str, TentacleConfigVariant] = Field(
        default_factory=dict,
        description="Tentacles keyed by deployment ID, with type selecting the implementation. "
        "The ID is shared by routing, channel profiles, and MCP installation.",
    )
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    logfire: LogfireConfig = Field(default_factory=LogfireConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    mcp_pool: McpPoolConfig = Field(default_factory=McpPoolConfig)
    oauth: OAuthConfig = Field(default_factory=OAuthConfig)
    auth: AuthConfig | None = Field(
        default=None,
        description="Local account credentials; required to sign in to Trunkline.",
    )
    projects: ProjectsConfig = Field(
        default_factory=ProjectsConfig,
        description=(
            "Declared code locations keyed by project name, reconciled into the "
            "registry at startup. Declaring one is the operator vouching for its "
            "contents, which reach an agent as instructions. Unrelated to "
            "`~/.claude/projects/`, which is transcript storage."
        ),
    )
    mirrors: MirrorsConfig = Field(
        default_factory=MirrorsConfig,
        description="How the declared projects' mirrors are synced.",
    )
    workspaces: WorkspacesConfig = Field(
        default_factory=WorkspacesConfig,
        description="When a thread's workspace is reclaimed.",
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

    @field_validator("tentacles")
    @classmethod
    def validate_unique_agent_runtimes(
        cls, tentacles: dict[str, TentacleConfigVariant]
    ) -> dict[str, TentacleConfigVariant]:
        """Agent runtimes own fixed native hook routes, so each is declared once."""
        runtimes: set[str] = set()
        for tentacle in tentacles.values():
            if isinstance(tentacle, AgentConfig):
                if tentacle.type in runtimes:
                    raise ValueError(f"duplicate agent runtime {tentacle.type!r}")
                runtimes.add(tentacle.type)
        return tentacles

    @model_validator(mode="after")
    def validate_oauth_configuration(self) -> Self:
        """Every enabled linked-account MCP tentacle stores credentials, and so does
        a Slack channel with an OAuth client: one of them needs the key."""
        enabled = [
            f"tentacles.{name}"
            for name, server in self.tentacles.items()
            if isinstance(server, OAuthMcpConfig) and server.enabled
        ] + [
            f"tentacles.{name}.oauth"
            for name, channel in self.tentacles.items()
            if isinstance(channel, SlackChannelConfig) and channel.oauth is not None
        ]
        if enabled and self.oauth.encryption_key is None:
            raise ValueError(
                f"oauth.encryption_key is required when {', '.join(enabled)} is enabled"
            )
        if self.oauth.callback_base_uri is None and any(
            isinstance(server, OAuthMcpConfig)
            and server.enabled
            and any(
                isinstance(flow, AuthorizationCodeFlowConfig) for flow in server.flows
            )
            for server in self.tentacles.values()
        ):
            raise ValueError(
                "oauth.callback_base_uri is required for authorization-code MCPs"
            )
        return self

    @model_validator(mode="after")
    def validate_channel_agent_routes(self) -> Self:
        """Every channel binding must name an enabled, configured agent."""
        configured = {
            name
            for name, tentacle in self.tentacles.items()
            if isinstance(tentacle, AgentConfig) and tentacle.enabled
        }
        errors: list[InitErrorDetails] = []
        for channel_id, channel in self.tentacles.items():
            if not isinstance(channel, ChannelConfig):
                continue
            for index, agent_id in enumerate(channel.agents):
                if agent_id not in configured:
                    errors.append(
                        InitErrorDetails(
                            type=PydanticCustomError(
                                "channel_agent_route",
                                "{agent} does not match a configured agent tentacle",
                                {"agent": repr(agent_id)},
                            ),
                            loc=("tentacles", channel_id, "agents", index),
                            input=agent_id,
                        )
                    )
        if errors:
            raise ValidationError.from_exception_data(type(self).__name__, errors)
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
        # One source over the whole search: packaged defaults, then the config
        # home's files. Read at call time rather than declared in `model_config`,
        # because the home depends on the environment and the working directory —
        # both of which a test moves after this class is imported.
        yaml_settings = YamlConfigSettingsSource(settings_cls, yaml_file=config_files())
        return (
            init_settings,
            env_settings,
            yaml_settings,
            dotenv_settings,
            file_secret_settings,
        )
