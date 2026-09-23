"""Discord outbound message shape."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import discord


@dataclass(frozen=True)
class DiscordOutboundMessage:
    """One message to send: its content, attachments, the users it may ping and
    an optional component view."""

    content: str = ""
    attachment_paths: tuple[Path, ...] = ()
    mentioned_user_ids: tuple[str, ...] = ()
    view: discord.ui.View | discord.ui.LayoutView | None = None
