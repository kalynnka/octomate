from __future__ import annotations

from pathlib import Path
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict

# The events this pipe registers and acts on. `UserPromptSubmit` and `Stop` carry the
# turn's prompt and answer — the whole human ledger — and `SessionEnd` bounds the
# session's lifecycle. Tool-lifecycle and message-display events are model-timeline
# detail rebuilt from the transcript on restore, not ingested live.
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


class ClaudeHookHandler(TypedDict):
    type: Literal["http"]
    url: str
    timeout: int


class ClaudeHookMatcher(TypedDict):
    hooks: list[ClaudeHookHandler]


def claude_hook_settings(url: str) -> dict[str, list[ClaudeHookMatcher]]:
    """The `hooks` fragment that pipes a native Claude session into Octomate.

    Merges into a Claude settings file (`~/.claude/settings.json` covers every project
    on the machine). Each handled event gets an `http` handler that POSTs the event
    body to `url` — Claude Code speaks HTTP natively, so no wrapper script is involved.

    The handlers are synchronous: Claude Code waits for the POST, which is what
    guarantees delivery before a short-lived `claude -p` process exits. They return an
    empty JSON object, so no hook decides anything — this pipe only observes.
    """
    return {
        event: [{"hooks": [{"type": "http", "url": url, "timeout": HOOK_TIMEOUT}]}]
        for event in HANDLED_HOOK_EVENTS
    }
