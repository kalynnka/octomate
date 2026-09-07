from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

# The route path, registered events, and hook timeout are the client-side contract,
# and live with the installer that writes them: `octomate_cli.tentacles.deepseek`.


class DeepseekHookInput(BaseModel):
    """A hook event from a native dsh session — POSTed by the emit command hook.

    dsh has no hook protocol of its own; its `dsh-hooks-claude-code` bridge
    plugin runs a Claude-Code-shaped hooks.json and speaks that dialect, so the
    fields here are Claude Code's. Two of that dialect's fields never arrive
    from dsh: there is no per-turn key (`prompt_id`), and `Stop` carries no
    `last_assistant_message` — the turn's answer exists only in the session
    log, which arrives through the stream (`octomate deepseek tail`, reading
    the client machine's dsh gateway).
    """

    model_config = ConfigDict(extra="ignore")

    hook_event_name: str
    session_id: str
    cwd: str = ""
    # What the human typed, on `UserPromptSubmit`. Recorded context only: the
    # ledger rows are written from the session log at turn close, where the
    # turn key exists.
    prompt: str | None = None
    # Where the bridge says the session log lives. Recorded context, never
    # followed — the log is zstd-framed, and the client-side tail reads its
    # machine's dsh gateway instead of the file.
    transcript_path: Path | None = None
    source: str | None = None
