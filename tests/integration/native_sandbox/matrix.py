"""The strict native audit contract, shared by all four real runtime runners."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from os.path import relpath
from pathlib import Path
from shlex import quote
from typing import Literal

SHELL_CHECKS = (
    "workspace_read",
    "workspace_write",
    "skill_read",
    "skill_cli",
    "outside_absolute_read",
    "outside_relative_read",
    "outside_symlink_read",
    "outside_write",
    "outside_symlink_write",
    "outside_execute",
    "outside_interpret",
    "outside_cwd",
    "skill_cli_outside_argument",
    "child_shell_outside_read",
    "shared_temp_read",
    "shared_temp_write",
)
FILE_CHECKS = ("workspace", "absolute", "traversal", "symlink")
SERVER_CANARY = "OCTOMATE_NATIVE_SANDBOX_CANARY"


@dataclass(frozen=True)
class ToolOutcome:
    text: str
    denied: bool = False


NativeCall = Callable[[Literal["shell", "file"], str], Awaitable[ToolOutcome]]


@dataclass(frozen=True)
class ShellProbe:
    command: str
    allowed: bool
    marker: str
    written_path: Path | None = None


@dataclass
class SandboxTree:
    workspace: Path
    outside: Path
    shared: Path
    marker: str

    def shell_probe(self, name: str) -> ShellProbe:
        """Keep outside user-data fixtures distinct from native temporary roots."""
        source = quote(str(self.outside / "canary.txt"))
        program = quote(str(self.outside / "program"))
        outside_write = self.outside / "written.txt"
        symlink_write = self.outside / "symlink-written.txt"
        temp_write = self.shared / "written.txt"
        commands = {
            "workspace_read": "cat input.txt",
            "workspace_write": f"printf %s {self.marker} > written.txt; cat written.txt",
            "skill_read": "cat .agents/skills/report/SKILL.md",
            "skill_cli": "./bin/report input.txt",
            "outside_absolute_read": f"cat {source}",
            "outside_relative_read": f"cat {quote(relpath(self.outside / 'canary.txt', self.workspace))}",
            "outside_symlink_read": "cat outside-link/canary.txt",
            "outside_write": f"printf %s {self.marker} > {quote(str(outside_write))}",
            "outside_symlink_write": f"printf %s {self.marker} > outside-link/symlink-written.txt",
            "outside_execute": program,
            "outside_interpret": f"/bin/sh {program}",
            "outside_cwd": f"cd {quote(str(self.outside))} && printf %s {self.marker}",
            "skill_cli_outside_argument": f"./bin/report {source}",
            "child_shell_outside_read": f"/bin/sh -c {quote('cat ' + source)}",
            "shared_temp_read": f"cat {quote(str(self.shared / 'canary.txt'))}",
            "shared_temp_write": f"printf %s {self.marker} > {quote(str(temp_write))}",
        }
        written = {
            "outside_write": outside_write,
            "outside_symlink_write": symlink_write,
            "shared_temp_write": temp_write,
        }
        return ShellProbe(
            command=commands[name],
            allowed=name in SHELL_CHECKS[:4],
            marker=self.marker,
            written_path=written.get(name),
        )

    def file_path(self, name: str) -> str:
        return {
            "workspace": "input.txt",
            "absolute": str(self.outside / "canary.txt"),
            "traversal": relpath(self.outside / "canary.txt", self.workspace),
            "symlink": "outside-link/canary.txt",
        }[name]
