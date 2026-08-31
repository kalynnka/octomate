"""A package manager no real tree has, for tests about installing rather than
about `uv` or `npm`.

Every command it runs appends a line to `ran.txt` in the tree, so counting those
lines counts the installs that actually happened — which is the question most of
these tests are asking: a copied fork must not install again, a mirror whose
lockfile stood still must not either, and a moved lockfile must.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest

from octomate.managers import dependencies
from octomate.managers.dependencies import MANAGERS, PackageManager

# One successful step of an install, and one that fails with something to log.
RAN = ("sh", "-c", "echo ran >> ran.txt")
FAILS = ("sh", "-c", "echo nope >&2; exit 1")


@dataclass
class Probe(PackageManager):
    """A manager whose install is whatever the test needs it to be."""

    lockfile: ClassVar[str] = "probe.lock"
    commands: tuple[tuple[str, ...], ...] = (RAN,)

    async def install(self, tree: Path) -> bool:
        for argv in self.commands:
            if not await self.run(tree, *argv):
                return False
        return True


def probing(monkeypatch: pytest.MonkeyPatch, *commands: tuple[str, ...]) -> None:
    """Put the probe last among the real managers, so a tree carrying `probe.lock`
    resolves to it while everything else keeps answering as it does in production.
    """
    monkeypatch.setattr(
        dependencies, "MANAGERS", (*MANAGERS, Probe(commands or (RAN,)))
    )


def runs(tree: Path) -> int:
    ran = tree / "ran.txt"
    return len(ran.read_text().splitlines()) if ran.is_file() else 0
