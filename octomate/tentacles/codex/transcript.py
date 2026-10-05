"""Typed lines of a Codex rollout file, and the runtime's own tree."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter

from octomate.types.json import JsonObject
from octomate.types.permissions import CodexPermissionMode

# Codex's own tree (`CODEX_HOME` relocates it): a workspace root under it is the
# runtime's per-session storage, never a project.
CODEX_HOME_DIRS: tuple[Path, ...] = (
    Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"),
)


class RolloutLine(BaseModel):
    """One line of a Codex rollout: its timestamp, record type, and raw payload."""

    model_config = ConfigDict(extra="ignore")

    timestamp: AwareDatetime
    type: Literal[
        "session_meta",
        "turn_context",
        "event_msg",
        "response_item",
        "world_state",
        "compacted",
        "inter_agent_communication_metadata",
    ]
    payload: JsonObject


rollout_line_adapter = TypeAdapter(RolloutLine)


class ThreadSpawnMetadata(BaseModel):
    """A child rollout's `thread_spawn` metadata, naming the parent thread."""

    model_config = ConfigDict(extra="ignore")

    parent_thread_id: str
    depth: int = 1
    agent_path: str = ""


class SubagentSource(BaseModel):
    """The `subagent` block of a session's `source`, present on a spawned child."""

    model_config = ConfigDict(extra="ignore")

    thread_spawn: ThreadSpawnMetadata | None = None


class SessionSource(BaseModel):
    """A structured `session_meta` `source`, carrying a spawned child's lineage."""

    model_config = ConfigDict(extra="ignore")

    subagent: SubagentSource | None = None


class SessionMetadata(BaseModel):
    """The payload of a rollout's opening `session_meta` line."""

    model_config = ConfigDict(extra="ignore")

    session_id: str
    id: str | None = None
    # The directory the session opened in. Codex repeats it per turn in a
    # `turn_context` line, which across every rollout on hand never disagrees with
    # this one, so the session's own report is the whole of where its turns ran.
    cwd: str = ""
    originator: str | None = None
    source: str | SessionSource | None = None
    thread_source: str | None = None


session_metadata_adapter = TypeAdapter(SessionMetadata)


class WorkspaceWritePolicy(BaseModel):
    """The workspace sandbox representable by Octomate's review presets."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["workspace-write"]
    writable_roots: list[str] = Field(default_factory=list, max_length=0)
    network_access: Literal[False] = False
    exclude_tmpdir_env_var: Literal[False] = False
    exclude_slash_tmp: Literal[False] = False


class FullAccessPolicy(BaseModel):
    """The unrestricted sandbox used by the full-access preset."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["danger-full-access"]


class TurnPermissions(BaseModel):
    """Permissions recorded in a native rollout's turn_context payload."""

    approval_policy: Literal["on-request", "never"]
    approvals_reviewer: Literal["user", "auto_review"] = "user"
    sandbox_policy: WorkspaceWritePolicy | FullAccessPolicy = Field(
        discriminator="type"
    )

    @property
    def permission_mode(self) -> CodexPermissionMode:
        if isinstance(self.sandbox_policy, FullAccessPolicy):
            if self.approval_policy == "never":
                return "full_access"
        elif self.approval_policy == "on-request":
            return (
                "auto_review"
                if self.approvals_reviewer == "auto_review"
                else "user_review"
            )
        raise ValueError("Native Codex permissions do not match an Octomate preset")


def payload_type(line: RolloutLine) -> str | None:
    value = line.payload.get("type")
    return value if isinstance(value, str) else None
