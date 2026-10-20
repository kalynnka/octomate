"""Configuration for serving ACP and connecting installed ACP harnesses."""

from typing import Literal

from anyio import Path as AsyncPath
from pydantic import BaseModel, Field, SecretStr

from octomate.config.agents.common import AgentConfig


class AcpProvider(BaseModel):
    """An installed harness speaking ACP v1 over stdio on the server."""

    command: str = Field(
        min_length=1,
        description="Executable path or command on PATH; never installed automatically.",
    )
    args: list[str] = Field(
        default_factory=list,
        description="Arguments passed directly to the executable, without a shell.",
    )
    env: dict[str, SecretStr] = Field(
        default_factory=dict,
        description="Environment overrides added to the ACP SDK's inherited environment.",
    )
    auth_method: str | None = Field(
        default=None,
        description="Advertised ACP authentication method to invoke after initialization. "
        "Authenticate the harness on the server beforehand; interactive login is not relayed.",
    )

    @staticmethod
    async def resolve_workspace(
        cwd: str,
        additional_directories: list[str] | None = None,
    ) -> tuple[str, list[str] | None]:
        """Resolve absolute server paths, including symbolic-link aliases."""
        if any(
            not AsyncPath(path).is_absolute()
            for path in [cwd, *(additional_directories or [])]
        ):
            raise ValueError("ACP workspace paths must be absolute")
        return (
            str(await AsyncPath(cwd).resolve()),
            [str(await AsyncPath(path).resolve()) for path in additional_directories]
            if additional_directories is not None
            else None,
        )


class AcpChannelConfig(BaseModel, extra="forbid"):
    """Enable the single inbound ACP API under a deployment identity."""

    type: Literal["acp"] = "acp"
    enabled: bool = Field(
        default=True,
        description="Register the inbound ACP channel and mount its API routes.",
    )


class AcpAgentConfig(AgentConfig):
    """Connect an independently named outbound ACP harness.

    Each harness is an agent backend and mounts no routes, so several may coexist.
    Startup opens an unprompted provider session in a temporary directory to
    discover its model config options; the harness may retain that empty session.
    """

    type: Literal["acp_agent"] = "acp_agent"
    gateway: bool = Field(
        default=True,
        description="Supply the active conversation's Octomate MCP to outbound runs. "
        "Requires HTTP MCP support; false omits Octomate tools.",
    )
    provider: AcpProvider = Field(
        description="Installed ACP harness to run as an agent.",
    )
