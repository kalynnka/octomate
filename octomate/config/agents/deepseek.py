"""DeepSeek Harness configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
)
from pydantic_ai.settings import ThinkingEffort

from octomate.config.agents.common import AgentConfig, Claim
from octomate.types.permissions import DeepseekPermissionMode

# A filesystem path from config, with `~` meaning what the person writing it meant:
# pydantic keeps `~/...` literal, and `Path("~/x").resolve()` yields `<cwd>/~/x` rather
# than a home directory — a root like that matches nothing and quietly stops a session
# being ingested.
type ConfigPath = Annotated[Path, AfterValidator(Path.expanduser)]


class DeepseekConfig(AgentConfig):
    """WIP DeepSeek Harness runner, registered as the `deepseek` agent tentacle.

    The tentacle owns a `dsh web` child with a private configuration home,
    sharing settings and session data with the configured native home.
    It drives the harness over the `/api` gateway — HTTP for unary calls,
    the mux WebSocket for events — the
    same integration surface dsh's own web client uses. Sessions are
    per-conversation: the dsh session id is stored as the conversation
    `external_id` and prompted again for later turns.
    """

    model_config = ConfigDict(extra="ignore")

    type: Literal["deepseek"] = "deepseek"

    instrument: bool = Field(
        default=False,
        description="Record DeepSeek session events under the driving Logfire trace.",
    )
    host: Literal["127.0.0.1", "localhost"] = Field(
        default="127.0.0.1",
        description=(
            "Where a dsh serves `/api` — loopback only, enforced here: the "
            "gateway uses HTTP, and a started child binds loopback, "
            "so a remote host could neither be trusted nor answered."
        ),
    )
    port: int = Field(
        default=3081,
        ge=1,
        le=65535,
        description=(
            "Port for Octomate's own DSH child, separate from native DSH's 3080. "
            "An occupied port fails startup; existing runtimes are never attached."
        ),
    )
    browser_url: HttpUrl | None = Field(
        default=None,
        description=(
            "Browser origin of a reverse proxy, e.g. https://dsh.example:8443. "
            "A started child trusts this authority and prints its login link "
            "to the console once. Without this setting the printed link uses "
            "the child's loopback URL. "
            "Configure the proxy separately; the child still binds loopback."
        ),
    )

    executable: str = Field(
        default="dsh",
        description=(
            "The dsh command to spawn `dsh web` — a name resolved on PATH or "
            "an absolute path to a built dsh."
        ),
    )
    extra_args: list[str] = Field(
        default_factory=list,
        description=(
            "Extra arguments placed after `web` and before the web app's "
            "`--host`, `--port`, and `--no-open` flags, e.g. a `--patch` "
            "overlay managed by Octomate. A dsh that "
            "refuses one of these exits and fails the start — only octomate's "
            "own `--no-open` is dropped and retried."
        ),
    )
    dsh_home: ConfigPath = Field(
        default=Path("~/.dsh"),
        # The default rides through ConfigPath's expanduser like any set value.
        validate_default=True,
        description=(
            "Native DSH home whose settings.yaml, .credentials.yaml, sessions "
            "and attachments are shared with Octomate's child. Its plugins, "
            "hooks, MCPs and profile patches are not loaded. The child receives "
            "a separate temporary DSH_HOME."
        ),
    )
    claims: dict[str, Claim] = Field(
        default_factory=dict,
        description="Metadata for models whose harness omits descriptions or effort "
        "capabilities. Keys are provider-qualified model names.",
    )
    efforts: dict[ThinkingEffort, str] = Field(
        default_factory=lambda: {
            "minimal": "off",
            "low": "off",
            "medium": "high",
            "high": "high",
            "xhigh": "max",
        },
        description=(
            "Octomate's one effort vocabulary mapped onto dsh's adapter-owned "
            "reasoning-effort ids. The default fits llm-deepseek (off/high/max); "
            "a deployment routing another adapter overrides it."
        ),
    )
    permission_mode: DeepseekPermissionMode = Field(
        default="workspace-write",
        description=(
            "Permission preset a dsh conversation falls back to when it carries "
            "none of its own. dsh's preset bundles sandbox mode and approval "
            "policy; switched per session via the `/permission` command."
        ),
    )
    agent_preset: str | None = Field(
        default=None,
        description=(
            "Agent preset new sessions are composed from (`session/create`'s "
            "agentPreset). Null takes the deployment's default preset."
        ),
    )
    approval_timeout: float | None = Field(
        default=3600.0,
        description=(
            "Seconds to wait for a human approval/answer before the card expires "
            "and the dsh request is answered `cancelled` (so the turn unblocks). An "
            "hour by default, because not answering is the ordinary case rather than "
            "the exotic one, and an unbounded wait leaves the thread unusable for "
            "good. None waits indefinitely."
        ),
    )
    ready_timeout: float = Field(
        default=60.0,
        gt=0,
        description=(
            "Seconds to wait for the `dsh web:` readiness banner before the "
            "spawn is declared failed."
        ),
    )

    @field_validator("browser_url")
    @classmethod
    def browser_origin(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and (
            value.username is not None
            or value.password is not None
            or value.path not in {None, "/"}
            or value.query is not None
            or value.fragment is not None
        ):
            raise ValueError(
                "browser_url must be an origin without credentials, path, query, or fragment"
            )
        return value
