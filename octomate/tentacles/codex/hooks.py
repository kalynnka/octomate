"""The hook payload POSTed back from a native Codex session."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

# The route path, registered events, and hook timeout are the client-side contract,
# and live with the installer that writes them: `octomate_cli.tentacles.codex`.


class CodexHookInput(BaseModel):
    """A hook event from a native Codex session, as the hook route validates it."""

    model_config = ConfigDict(extra="ignore")

    hook_event_name: str
    session_id: str
    session_name: str | None = Field(
        default=None,
        description="Session title read by the hook client through the Codex SDK.",
    )
    transcript_path: Path | None = None
    cwd: str = ""
    turn_id: str | None = None
    prompt: str | None = None
    last_assistant_message: str | None = None
    agent_id: str | None = None
    agent_type: str | None = None
    agent_transcript_path: Path | None = None
    source: str | None = None
