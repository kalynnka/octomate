"""Configuration file operations shared by the deepseek tentacle's installers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

MARK_BEGIN = "# >>> octomate deepseek hooks >>>"


MARK_END = "# <<< octomate deepseek hooks <<<"


DshHomeOption = Annotated[
    Path | None,
    typer.Option(help="The dsh home to install into; defaults to $DSH_HOME or ~/.dsh."),
]


def dsh_home(path: Path | None) -> Path:
    if path is not None:
        return path
    env = os.environ.get("DSH_HOME")
    return Path(env).expanduser() if env else Path.home() / ".dsh"


def patch_file(home: Path) -> Path:
    return home / "cordis.patch.yml"


def without_block(text: str, begin: str = MARK_BEGIN, end: str = MARK_END) -> str:
    """The patch file's text with one marker block removed, everything else
    kept byte-for-byte. Defaults to the hooks block's markers; the gateway
    block passes its own."""
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped == begin:
            inside = True
            continue
        if stripped == end:
            inside = False
            continue
        if not inside:
            kept.append(line)
    return "".join(kept)


def patch_text_with_block(
    text: str, block: str, begin: str = MARK_BEGIN, end: str = MARK_END
) -> str:
    """The patch file's text with our block installed exactly once.

    The file is a top-level YAML array. dsh's default is a lone `[]` flow
    document, which nothing can be appended after — that line is replaced by
    the block. A file already carrying block-sequence entries gets the block
    appended; a re-install replaces the existing block in place.
    """
    remainder = without_block(text, begin, end)
    lines = remainder.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip() == "[]":
            return "".join([*lines[:index], block, *lines[index + 1 :]])
    if remainder and not remainder.endswith("\n"):
        remainder += "\n"
    return remainder + block


def patch_text_without_block(
    text: str, begin: str = MARK_BEGIN, end: str = MARK_END
) -> str:
    """The uninstall splice: the block removed, and the empty-array document
    restored when nothing else remains — a comments-only file parses as null,
    not the empty entry list the loader expects."""
    remainder = without_block(text, begin, end)
    if any(
        line.strip() and not line.strip().startswith("#")
        for line in remainder.splitlines()
    ):
        return remainder
    if remainder and not remainder.endswith("\n"):
        remainder += "\n"
    return remainder + "[]\n"
