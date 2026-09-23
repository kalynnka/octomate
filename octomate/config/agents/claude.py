"""Claude Code configuration."""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from octomate.config.agents.common import AgentConfig, Claim
from octomate.types.permissions import ClaudePermissionMode

logger = logging.getLogger(__name__)


class ClaudeSSHConfig(BaseModel):
    """Remote-host settings for the Claude tentacle.

    The SSH transport is parked: nothing constructs it, so a block here is warned
    about and not honoured — the run stays local. See `ClaudeCodeConfig.ssh`.
    """

    host: str
    identity_file: str | None = None
    ssh_options: list[str] = Field(default_factory=list)
    claude_bin: str = "claude"


class ClaudeCodeConfig(AgentConfig):
    """Claude Agent SDK runner, selected by `type: claude`.

    Opt-in: the agent is absent unless a block is supplied. The CLI supplies the
    model catalog. `claude` runs as a local subprocess; an `ssh` block is warned
    about and not honoured while the remote transport is parked.
    """

    model_config = ConfigDict(extra="ignore")

    type: Literal["claude"] = "claude"

    enabled: bool = Field(
        default=True,
        description="Whether to register the Claude tentacle when the config block exists.",
    )
    instrument: bool = Field(
        default=False,
        description="Export native Claude OTLP spans to Octomate's Logfire project "
        "under the driving trace.",
    )
    claims: dict[str, Claim] = Field(
        default_factory=dict,
        description="Metadata for models whose harness omits descriptions or effort "
        "capabilities. Keys are provider-qualified model names.",
    )
    permission_mode: ClaudePermissionMode = Field(
        default="default",
        description=(
            "Approval posture for a Claude conversation whose thread is in no "
            "project, or whose project declares none. Handed to the SDK verbatim."
        ),
    )
    max_turns: int | None = None
    ssh: ClaudeSSHConfig | None = Field(
        default=None,
        description=(
            "Remote host to run `claude` on. Disabled, and no longer wired: a "
            "run happens in its thread's workspace, and there is nothing that "
            "makes a workspace on another machine. The tentacle hands the SDK "
            "no transport at all now; `SSHTransport` is kept as it stands, but "
            "nothing constructs it. Re-enabling is three things rather than "
            "one — somewhere remote to fork a workspace into, a directory on "
            "`ClaudeSSHConfig` to name it, and the transport wired back in. "
            "Setting it is warned about rather than refused: with the transport "
            "parked the block reaches nothing, so failing a start over it would "
            "cost more than it saves."
        ),
    )
    approval_timeout: float | None = Field(
        default=3600.0,
        description=(
            "Seconds to wait for a human approval/answer before the card expires "
            "and the pending tool is denied (so the live run unblocks). An hour by "
            "default, because not answering is the ordinary case rather than the "
            "exotic one, and an unbounded wait leaves the thread unusable for good. "
            "None waits indefinitely."
        ),
    )

    @field_validator("ssh")
    @classmethod
    def warn_remote_runs_are_off(
        cls, ssh: ClaudeSSHConfig | None
    ) -> ClaudeSSHConfig | None:
        """Say that a configured remote host is not honoured, and keep it as written.

        This refused the whole config while the tentacle still built an SSH
        transport, since a block that would have been obeyed had to be stopped
        loudly. The transport is parked now and the block reaches nothing, so the
        value is left as the operator wrote it and only the effect is reported.
        """
        if ssh is not None:
            logger.warning(
                "tentacles.claude.ssh is not honoured and the run stays local: a run "
                "happens in its thread's workspace, and nothing makes one on %s",
                ssh.host,
            )
        return ssh
