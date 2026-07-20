from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from octomate.types.json import JsonObject

# Where Codex writes rollout transcripts (dated dirs beneath). `CODEX_HOME` relocates
# the tree and names a single directory, unlike Claude's comma-separated
# `CLAUDE_CONFIG_DIR` — but the tuple shape matches, so both ingests gate the same way.
# The default, not the law: override with `agents.codex.transcript_root`.
CODEX_SESSIONS_DIRS: tuple[Path, ...] = (
    Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions",
)


class RolloutLine(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timestamp: datetime
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
    model_config = ConfigDict(extra="ignore")

    parent_thread_id: str
    depth: int = 1
    agent_path: str = ""


class SubagentSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    thread_spawn: ThreadSpawnMetadata | None = None


class SessionSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    subagent: SubagentSource | None = None


class SessionMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str
    id: str | None = None
    originator: str | None = None
    source: str | SessionSource | None = None
    thread_source: str | None = None


session_metadata_adapter = TypeAdapter(SessionMetadata)


def payload_type(line: RolloutLine) -> str | None:
    value = line.payload.get("type")
    return value if isinstance(value, str) else None
