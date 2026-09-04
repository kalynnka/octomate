from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import discord


@dataclass(frozen=True)
class DiscordOutboundMessage:
    content: str = ""
    attachment_paths: tuple[Path, ...] = ()
    mentioned_user_ids: tuple[str, ...] = ()
    view: discord.ui.View | None = None
