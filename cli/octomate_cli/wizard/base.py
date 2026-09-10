from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

import questionary
import typer
from prompt_toolkit.output import ColorDepth
from pydantic import TypeAdapter
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
    configure: Callable[[Console], T]
    checked: bool = False


def select_many(
    message: str, choices: list[questionary.Choice], *, required: bool = False
) -> list[str]:
    answer = questionary.checkbox(
        message,
        choices=choices,
        style=selection_style,
        instruction="(↑/↓ move · Space select · Enter continue)",
        validate=lambda selected: (
            bool(selected) or "Select at least one tentacle." if required else True
        ),
        color_depth=ColorDepth.DEPTH_1_BIT
        if "NO_COLOR" in os.environ
        else ColorDepth.TRUE_COLOR,
    ).ask()
    if answer is None:
        raise typer.Abort()
    return TypeAdapter(list[str]).validate_python(answer)


def select_tentacles[T](
    setups: dict[str, TentacleSetup[T]],
    selected: list[str] | None,
    *,
    console: Console,
    prompt: str,
    invalid_message: str,
    required: bool = False,
) -> list[T]:
    if selected is None:
        selected = select_many(
            prompt,
            [
                questionary.Choice(setup.label, value=name, checked=setup.checked)
                for name, setup in setups.items()
            ],
            required=required,
        )
    if (required and not selected) or any(name not in setups for name in selected):
        raise typer.BadParameter(invalid_message)
    return [setups[name].configure(console) for name in dict.fromkeys(selected)]
