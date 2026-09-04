from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

# Which events the pipe registers, the route paths, and the hook timeout are the
# client-side contract, and live with the installer that writes them: `octomate_cli.claude`.


class ClaudeHookInput(BaseModel):
    """A hook event from a native Claude session — POSTed by the emit command hook —
    as FastAPI validates the request body.

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
    # the prompt with injected context blocks); on `SubagentStart`, the subagent's
    # opening prompt.
    prompt: str | None = None
    # The turn's final answer, on `Stop`; the subagent's, on `SubagentStop`.
    last_assistant_message: str | None = None
    # Present iff the event fired inside a subagent — the discriminator that keeps a
    # subagent's events away from the parent-turn handlers. Matches the child
    # transcript's filename (`agent-<agent_id>.jsonl`).
    agent_id: str | None = None
    agent_type: str | None = None
