from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

HOOK_TIMEOUT = 10
CODEX_HOOK_PATH = "/hooks/codex"
DRIVEN_ENV = "OCTOMATE_CODEX_DRIVEN"
HANDLED_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop")


class CodexHookInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hook_event_name: str
    session_id: str
    transcript_path: Path | None = None
    cwd: str = ""
    turn_id: str | None = None
    prompt: str | None = None
    last_assistant_message: str | None = None
    source: str | None = None
    octomate_driven: bool = False
