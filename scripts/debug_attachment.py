"""Switch the VS Code debug profile and native agent hooks and MCP entries."""

import argparse
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from octomate_cli.tentacles.claude import hooks as claude_hooks
from octomate_cli.tentacles.codex import hooks as codex_hooks


def main(mode: Literal["on", "off"]) -> None:
    workspace = Path.cwd()
    profile = (
        workspace / ".octomate" / f"cli.{'debug' if mode == 'on' else 'macmini'}.toml"
    ).resolve(strict=True)
    project_hooks: list[tuple[str, str, Path]] = []
    codex_path = workspace / ".codex" / "hooks.json"
    if any(
        codex_hooks.is_octomate_handler(hook)
        for groups in codex_hooks.HookSettings.load(codex_path).hooks.values()
        for group in groups
        for hook in group.hooks
    ):
        project_hooks.append(("codex", "--hooks-file", codex_path))
    for name in ("settings.json", "settings.local.json"):
        claude_path = workspace / ".claude" / name
        if any(
            claude_hooks.is_octomate_hook(hook)
            for groups in claude_hooks.HookSettings.load(claude_path).hooks.values()
            for group in groups
            for hook in group.hooks
        ):
            project_hooks.append(("claude", "--settings", claude_path))

    if mode == "on":
        cli = (workspace / ".venv" / "bin" / "octomate").resolve(strict=True)
    else:
        installed = subprocess.run(
            ["uv", "tool", "dir", "--bin"], check=True, capture_output=True, text=True
        )
        cli = (Path(installed.stdout.strip()) / "octomate").resolve(strict=True)

    shutil.copy2(profile, workspace / ".octomate" / "cli.toml")
    for agent in ("codex", "claude"):
        subprocess.run(
            [str(cli), agent, "hooks", "install", "--scope", "user"], check=True
        )
    for agent, option, path in project_hooks:
        subprocess.run(
            [str(cli), agent, "hooks", "install", option, str(path)], check=True
        )
    subprocess.run([str(cli), "codex", "mcp", "install"], check=True)
    subprocess.run(
        [str(cli), "claude", "mcp", "install", "--scope", "user"], check=True
    )
    print(f"Debug attachment {mode}: hooks and MCP use {cli}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("on", "off"))
    main(parser.parse_args().mode)
