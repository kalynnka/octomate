from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest
from octomate_cli.tentacles.claude import claude_typer
from octomate_cli.tentacles.codex import codex_typer
from typer.testing import CliRunner


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("existing_hooks", [False, True])
def test_hook_install_and_uninstall_preserve_user_configuration(
    tmp_path: Path, runtime: Literal["claude", "codex"], existing_hooks: bool
) -> None:
    app = claude_typer if runtime == "claude" else codex_typer
    option = "--settings" if runtime == "claude" else "--hooks-file"
    path = tmp_path / "settings.json"
    original = {
        "model": "custom-model",
        "permissions": {"allow": ["Read"], "custom": None},
    }
    group = {
        "matcher": "Bash",
        "custom": {"enabled": True},
        "hooks": [
            {"type": "command", "command": "echo keep", "async": True},
            {
                "type": "http",
                "url": "https://example.test/hook",
                "headers": {"X-Custom": "value"},
                "timeout": None,
            },
            {"type": "prompt", "prompt": "Check the result"},
            {"type": "future-handler", "options": {"custom": [1, None]}},
        ],
    }
    if existing_hooks:
        original["hooks"] = {"Stop": [group], "FutureEvent": [group]}
    path.write_text(json.dumps(original))
    runner = CliRunner()

    for _ in range(2):
        result = runner.invoke(app, ["hooks", "install", option, str(path)])
        assert result.exit_code == 0, result.output
    installed = json.loads(path.read_text())
    assert installed["model"] == original["model"]
    assert installed["permissions"] == original["permissions"]
    if existing_hooks:
        assert installed["hooks"]["Stop"][0] == group
        assert installed["hooks"]["FutureEvent"] == [group]

    result = runner.invoke(app, ["hooks", "uninstall", option, str(path)])

    assert result.exit_code == 0, result.output
    assert json.loads(path.read_text()) == original


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize(
    "contents",
    [
        "[]",
        '{"hooks": null}',
        '{"hooks": {"Stop": "invalid"}}',
        '{"hooks": {"Stop": [{"hooks": [{"type": "command"}]}]}}',
        '{"hooks": {"Stop": [{"hooks": [{"type": "http", "url": 42}]}]}}',
    ],
)
def test_invalid_hook_configuration_is_not_rewritten(
    tmp_path: Path, runtime: Literal["claude", "codex"], contents: str
) -> None:
    app = claude_typer if runtime == "claude" else codex_typer
    option = "--settings" if runtime == "claude" else "--hooks-file"
    path = tmp_path / "settings.json"
    path.write_text(contents)

    result = CliRunner().invoke(app, ["hooks", "install", option, str(path)])

    assert result.exit_code == 2
    assert "Invalid hook settings" in result.output
    assert path.read_text() == contents
