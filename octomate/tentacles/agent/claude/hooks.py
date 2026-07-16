from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

# The events this pipe registers and acts on. `UserPromptSubmit` and `Stop` carry the
# turn's prompt and answer — the whole human ledger — while `SessionEnd` closes the
# session so the transcript tailer can finalize. Tool-lifecycle and message-display
# events are model-timeline detail the tailer reads off the transcript, not ingested
# from the events.
#
# `SessionStart` is absent on purpose: Claude Code delivers it to `command` and
# `mcp_tool` hooks only, so registering it as `http` would install a handler that can
# never fire. The first prompt starts the tailer instead (see `ClaudeHookIngest`).
HandledHookEvent = Literal["UserPromptSubmit", "Stop", "SessionEnd"]
HANDLED_HOOK_EVENTS: tuple[HandledHookEvent, ...] = (
    "UserPromptSubmit",
    "Stop",
    "SessionEnd",
)

# Bound so a wedged or slow Octomate can never freeze someone's Claude session: past
# this the CLI abandons the hook and carries on.
HOOK_TIMEOUT = 10

# Where the tentacle mounts its hook router; a client's settings point at
# `http://<host>:<port>{CLAUDE_HOOK_PATH}`.
CLAUDE_HOOK_PATH = "/hooks/claude"


class ClaudeHookInput(BaseModel):
    """A hook event Claude Code POSTs, as FastAPI validates the request body.

    The common envelope plus the two event-specific fields the live tier consumes;
    every other event-specific field is ignored (`extra="ignore"`), so one model
    validates every event without a per-event variant.
    """

    model_config = ConfigDict(extra="ignore")

    hook_event_name: str
    session_id: str
    cwd: str = ""
    # Stable across every event of one turn, from UserPromptSubmit through Stop; the
    # per-turn key the human-ledger rows are stamped with and restore binds against.
    prompt_id: str | None = None
    # Unused live — this pipe never reads the transcript. Restore locates it to hydrate.
    transcript_path: Path | None = None
    # What the human typed, on `UserPromptSubmit` (the clean copy — the transcript pads
    # the prompt with injected context blocks).
    prompt: str | None = None
    # The turn's final answer, on `Stop`.
    last_assistant_message: str | None = None
