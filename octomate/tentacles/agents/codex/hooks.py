from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

# The route path, registered events, and hook timeout are the client-side contract,
# and live with the installer that writes them: `octomate_cli.codex`.
DRIVEN_ENV = "OCTOMATE_CODEX_DRIVEN"


class CodexHookInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hook_event_name: str
    session_id: str
    transcript_path: Path | None = None
    cwd: str = ""
    turn_id: str | None = None
    prompt: str | None = None
    last_assistant_message: str | None = None
    agent_id: str | None = None
    agent_type: str | None = None
    agent_transcript_path: Path | None = None
    source: str | None = None
    octomate_driven: bool = False
