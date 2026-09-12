from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import questionary
from rich.console import Console
from rich.theme import Theme

brand_color = "#5BA3B5"
console = Console(
    stderr=True,
    markup=False,
    highlight=False,
    theme=Theme(
        {
            "prompt": "bold",
            "prompt.default": "dim",
            "prompt.choices": "dim",
            "status.spinner": brand_color,
        }
    ),
)
selection_style = questionary.Style(
    [
        ("qmark", f"fg:{brand_color}"),
        ("question", "bold"),
        ("answer", "fg:ansigreen"),
        ("pointer", f"fg:{brand_color} bold"),
        ("highlighted", f"fg:{brand_color} bold"),
        ("selected", "fg:ansigreen"),
        ("text", ""),
        ("instruction", "fg:ansibrightblack"),
        ("separator", "fg:ansibrightblack"),
    ]
)


@dataclass(frozen=True)
class TentacleSetup[T]:
    label: str
    capabilities: tuple[Literal["Agent", "Channel", "MCP"], ...]
    configure: Callable[[Console], T]
    checked: bool = False
