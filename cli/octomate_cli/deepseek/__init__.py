"""`octomate deepseek ...` — the client's half of a native dsh session: the
commands that install the hooks, and the tail that streams the transcript.

A package rather than one module because the tail is not the installer. It pulls
in `websockets` and speaks to this machine's dsh gateway, neither of which an
install or an uninstall touches, so `base.py` imports it inside the one command
that tails. Re-exporting `base` alone is what keeps that deferral true: importing
this package to reach a hook path must not cost a websocket stack.
"""

from __future__ import annotations

from octomate_cli.deepseek.base import (
    DEEPSEEK_HOOK_PATH,
    DEEPSEEK_STREAM_PATH,
    deepseek_typer,
)

__all__ = [
    "DEEPSEEK_HOOK_PATH",
    "DEEPSEEK_STREAM_PATH",
    "deepseek_typer",
]
