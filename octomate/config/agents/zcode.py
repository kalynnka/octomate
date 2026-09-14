"""ZCode desktop runtime configuration."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal

from pydantic import Field

from octomate.config.agents.common import AgentConfig, AgentRouteModelName, Claim
from octomate.config.agents.deepseek import ConfigPath
from octomate.types.permissions import ZcodePermissionMode


class ZcodeConfig(AgentConfig):
    """Driven ZCode sessions using the runtime bundled with the desktop app."""

    id: ClassVar[str] = "zcode"

    gateway: Literal[False] = Field(
        default=False,
        description="Gateway tools are not supported by the ZCode runner yet.",
    )
    command: list[str] = Field(
        default_factory=lambda: [
            "node",
            "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
            "app-server",
            "--stdio",
        ],
        min_length=1,
        description="Complete argument vector used to launch the stdio app-server.",
    )
    desktop_config: ConfigPath = Field(
        default=Path("~/.zcode/v2/config.json"),
        validate_default=True,
        description="Read-only source of desktop provider and model configuration.",
    )
    state_dir: ConfigPath = Field(
        default=Path("~/.octomate/zcode"),
        validate_default=True,
        description="Octomate-owned directory for ZCode runtime state and its session database.",
    )
    provider: str = Field(
        default="builtin:bigmodel",
        min_length=1,
        description="Provider identifier in the ZCode desktop configuration.",
    )
    claims: dict[AgentRouteModelName, Claim] = Field(
        default_factory=dict,
        description="Per-model routing abilities and effort levels.",
    )
    permission_mode: ZcodePermissionMode = Field(
        default="build",
        description="Default ZCode permission posture.",
    )
    approval_timeout: float | None = Field(
        default=3600.0,
        gt=0,
        description="Seconds to wait for a human approval or answer before the card expires. None waits indefinitely.",
    )
    request_timeout: float = Field(
        default=60.0,
        gt=0,
        description="Timeout for app-server RPC responses, not for model turns.",
    )
