"""DeepSeek Harness configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, ClassVar, Literal, TypeAlias

from pydantic import AfterValidator, ConfigDict, Field
from pydantic_ai.settings import ThinkingEffort

from octomate.config.agents.common import AgentConfig, Claim
from octomate.types.permissions import DeepseekPermissionMode

# A filesystem path from config, with `~` meaning what the person writing it meant:
# pydantic keeps `~/...` literal, and `Path("~/x").resolve()` yields `<cwd>/~/x` rather
# than a home directory — a root like that matches nothing and quietly stops a session
# being ingested.
ConfigPath: TypeAlias = Annotated[Path, AfterValidator(Path.expanduser)]


class DeepseekConfig(AgentConfig):
    """DeepSeek Harness runner, registered as the `deepseek` agent tentacle.

    Opt-in: `agents.deepseek` is null by default, so the agent is absent unless a
    block is supplied. The tentacle attaches to a dsh already serving
    `host:port` — one the operator runs — and starts its own `dsh web` child
    only when nothing answers there. Either way it drives the harness over the
    `/api` gateway — HTTP for unary calls, the mux WebSocket for events — the
    same integration surface dsh's own web client uses. Sessions are
    per-conversation: the dsh session id is stored as the conversation
    `external_id` and prompted again for later turns.
    """

    model_config = ConfigDict(extra="ignore")

    id: ClassVar[str] = "deepseek"

    host: Literal["127.0.0.1", "localhost"] = Field(
        default="127.0.0.1",
        description=(
            "Where a dsh serves `/api` — loopback only, enforced here: the "
            "gateway has no TLS and no auth, and a started child binds loopback, "
            "so a remote host could neither be trusted nor answered. A dsh "
            "already answering here is attached to as it stands."
        ),
    )
    port: int = Field(
        default=3080,
        ge=1,
        le=65535,
        description=(
            "The `/api` port — dsh's own default bind. A started `dsh web` binds "
            "this same port, fixed rather than ephemeral, so the next probe "
            "attaches to it instead of starting a second writer of one DSH_HOME."
        ),
    )
    executable: str = Field(
        default="dsh",
        description=(
            "The dsh command to spawn `dsh web` with when nothing serves "
            "`host:port` — a name resolved on PATH or an absolute path to a "
            "built dsh."
        ),
    )
    extra_args: list[str] = Field(
        default_factory=list,
        description=(
            "Extra arguments appended after "
            "`web --host 127.0.0.1 --port <port> --no-open`, e.g. a `--patch` "
            "overlay. Only applies to a harness octomate starts. A dsh that "
            "refuses one of these exits and fails the start — only octomate's "
            "own `--no-open` is dropped and retried."
        ),
    )
    dsh_home: ConfigPath = Field(
        default=Path("~/.dsh"),
        # The default rides through ConfigPath's expanduser like any set value.
        validate_default=True,
        description=(
            "DSH_HOME for a harness octomate starts — where dsh keeps its "
            "sessions and settings. Defaults to dsh's own ~/.dsh; the child "
            "always receives this value verbatim. An attached harness keeps "
            "whatever home it was started with."
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
            "Agent preset new sessions are composed from (`session.create`'s "
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
