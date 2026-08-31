"""The deployment: what a config home is, and the settings class it validates into.

A deployment is a *config home* — one directory holding one flat file per subsystem,
so a change to channels touches `channels.yaml` and nothing else. Each file's
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
They are layered per top-level key and wholesale — a home that declares `agents:`
replaces the default `agents:` entirely rather than merging into it, which is the
behaviour `octomate.default.yaml` and `octomate.yaml` had between them.
"""

from __future__ import annotations

import os
from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated, Self

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

from octomate.config.agents import AgentsConfig
from octomate.config.channels import ChannelConfigVariant
from octomate.config.integrations import IntegrationConfig
from octomate.config.mcp import McpServerConfig
from octomate.config.mirrors import MirrorsConfig
from octomate.config.oauth import OAuthConfig
from octomate.config.observability import LogfireConfig, LoggingConfig
from octomate.config.projects import ProjectsConfig
from octomate.config.providers import ProvidersConfig
from octomate.config.users import UsersConfig
from octomate.config.workspaces import WorkspacesConfig
from octomate.schemas.project import Project

OCTOMATE_HOME_ENV = "OCTOMATE_HOME"

# One file per subsystem, in the order they are read. `octomate.yaml` carries the
# host's own settings (host, port, mcp_path, db_url) and comes first so a later
# file cannot be shadowed by it.
CONFIG_FILES: tuple[str, ...] = (
    "octomate.yaml",
    "agents.yaml",
    "channels.yaml",
    "users.yaml",
    "projects.yaml",
    "providers.yaml",
    "integrations.yaml",
    "mcp.yaml",
    "observability.yaml",
    "oauth.yaml",
)

DEFAULTS_DIR = Path(__file__).parent / "defaults"


def config_home() -> Path:
    """The directory this process reads its deployment from.

    Returned whether or not it exists — a machine with no config at all still names
    a home, which is what `octomate init` writes into and what a boot error can say.
    """
    from_env = os.environ.get(OCTOMATE_HOME_ENV)
    if from_env:
        return Path(from_env).expanduser()
    candidates = (
        Path.cwd() / ".octomate" / "config",
        Path.home() / ".octomate" / "config",
    )
    for candidate in candidates:
        if any((candidate / name).is_file() for name in CONFIG_FILES):
            return candidate
    return candidates[-1]


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

    mcp_path: Annotated[
        str,
        Field(
            description="The MCP endpoint under each served server's mount: the "
            "gateway answers at `/gateway` followed by this path."
        ),
    ] = "/mcp"

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    logfire: LogfireConfig = Field(default_factory=LogfireConfig)
    channels: dict[str, ChannelConfigVariant] = Field(
        default_factory=dict,
        description=(
            "Channel tentacles keyed by instance id, `type` selecting the platform — "
            "so one platform can be mounted more than once, a key per app. The key is "
            "the channel tentacle id throughout: what `users[].profiles` names, and "
            "what a thread records as its origin."
        ),
    )
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
    users: UsersConfig = Field(
        default_factory=UsersConfig,
        description=(
            "Registered cross-channel users keyed by stable username; profiles are "
            "reconciled into the registry at startup."
        ),
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
        """Every channel route must name an agent that is configured, and a model
        that agent actually offers.

        Neither half names a channel or an agent: the channels are whatever the map
        holds, and `AgentsConfig.configured_models` owns the knowledge of which
        agents exist and where each keeps its model names. Adding a platform or an
        agent therefore changes one place, not this one.
        """
        configured = self.agents.configured_models()
        errors: list[InitErrorDetails] = []

        for channel_id, channel in self.channels.items():
            for index, route in enumerate(channel.agents):
                location = ("channels", channel_id, "agents", index)
                models = configured.get(route.agent)
                if models is None:
                    # One message whether the name is a typo or an agent left
                    # undeclared: from a route's point of view there is no
                    # difference, and both are fixed in the same two places.
                    errors.append(
                        InitErrorDetails(
                            type=PydanticCustomError(
                                "channel_agent_route",
                                "{agent} does not match a configured agent tentacle",
                                {"agent": repr(route.agent)},
                            ),
                            loc=(*location, "agent"),
                            input=route.agent,
                        )
                    )
                    continue
                if route.model not in models:
                    errors.append(
                        InitErrorDetails(
                            type=PydanticCustomError(
                                "channel_agent_route",
                                "{model} is not configured in agents.{agent}.models",
                                {"model": repr(route.model), "agent": route.agent},
                            ),
                            loc=(*location, "model"),
                            input=route.model,
                        )
                    )
        if errors:
            raise ValidationError.from_exception_data(type(self).__name__, errors)
        return self

    @model_validator(mode="after")
    def validate_user_links(self) -> Self:
        """A typo'd channel id in a user's links must fail the boot, not
        silently produce a link no channel will ever resolve.

        The native pseudo-channels are not admissible either: a native session
        is registered by the user's own `secret`, which anchors it on a
        transient profile — no claimed row exists for a link to seed, so a
        pseudo-channel link is as unresolvable as any typo."""
        errors: list[InitErrorDetails] = [
            InitErrorDetails(
                type=PydanticCustomError(
                    "user_link_channel",
                    "{channel} does not match a configured channel",
                    {"channel": repr(channel_id)},
                ),
                loc=("users", username, "profiles", channel_id),
                input=profile.channel_user_id,
            )
            for username, user in self.users.items()
            for channel_id, profile in user.profiles.items()
            if channel_id not in self.channels
        ]
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
