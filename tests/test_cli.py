"""The operator CLI's hook install/uninstall merge behavior."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from octomate_cli.claude import LAUNCH_SCRIPT, claude_typer
from octomate_cli.cli import app
from octomate_cli.codex import EMIT_SCRIPT, codex_typer
from pydantic import SecretStr
from typer.testing import CliRunner

runner = CliRunner()
URL = "http://127.0.0.1:9999/hooks/claude"
CODEX_URL = "http://127.0.0.1:9999/hooks/codex"
COMMAND_HOOK = {"type": "command", "command": "echo done"}


def settings_with(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(document))
    return path


# Typing this `JsonObject` satisfies ANN401 and costs 72 pyright errors: every
# `document["hooks"]["Stop"]` below then subscripts a `JsonValue` union. The document is
# a settings file this test wrote itself, and asserting on it is the whole point, so the
# dynamic type is the honest one here rather than a shape re-declared to please the rule.
def read(path: Path) -> Any:  # noqa: ANN401
    return json.loads(path.read_text())


def hook_types(groups: list[Any]) -> list[str]:
    return [hook["type"] for group in groups for hook in group["hooks"]]


def test_install_preserves_existing_hooks_and_is_idempotent(tmp_path: Path) -> None:
    path = settings_with(
        tmp_path,
        {"model": "opus", "hooks": {"Stop": [{"hooks": [COMMAND_HOOK]}]}},
    )

    for _ in range(2):  # running twice must not duplicate Octomate's handler
        result = runner.invoke(
            claude_typer, ["hooks", "install", "--url", URL, "--settings", str(path)]
        )
        assert result.exit_code == 0

    document = read(path)
    assert document["model"] == "opus"  # unrelated settings untouched
    hooks = document["hooks"]
    # The pre-existing command hook survives beside one Octomate http handler.
    assert hook_types(hooks["Stop"]) == ["command", "http"]
    # No SessionStart: Claude Code delivers it to command/mcp_tool hooks only, so an
    # http handler for it could never fire.
    assert set(hooks) == {
        "UserPromptSubmit",
        "Stop",
        "SessionEnd",
        "SubagentStart",
        "SubagentStop",
    }


def test_the_installed_handler_references_the_secret_rather_than_carrying_it(
    tmp_path: Path,
) -> None:
    """A settings file is a document people commit and share; the credential is not.
    Claude Code resolves `${VAR}` from the environment at fire time, and only for names
    listed in `allowedEnvVars`."""
    path = tmp_path / "settings.json"
    runner.invoke(
        claude_typer, ["hooks", "install", "--url", URL, "--settings", str(path)]
    )

    [handler] = [
        hook for group in read(path)["hooks"]["Stop"] for hook in group["hooks"]
    ]
    assert handler["headers"] == {"Authorization": "Bearer ${OCTOMATE__HOOK_SECRET}"}
    assert handler["allowedEnvVars"] == ["OCTOMATE__HOOK_SECRET"]


def test_install_retires_an_event_octomate_no_longer_registers(tmp_path: Path) -> None:
    """The shape an older version left behind: an unauthenticated handler on every
    event, SessionStart included. Re-installing must retire SessionStart rather than
    leave our stale handler on an event we no longer register."""
    stale = {"type": "http", "url": URL, "timeout": 10}
    path = settings_with(
        tmp_path,
        {
            "hooks": {
                "SessionStart": [{"hooks": [stale]}],
                "Stop": [{"hooks": [stale, COMMAND_HOOK]}],
            }
        },
    )

    runner.invoke(
        claude_typer, ["hooks", "install", "--url", URL, "--settings", str(path)]
    )

    hooks = read(path)["hooks"]
    assert "SessionStart" not in hooks
    # The operator's own hook on Stop survives beside exactly one fresh Octomate handler.
    assert hook_types(hooks["Stop"]) == ["command", "http"]
    [fresh] = [h for g in hooks["Stop"] for h in g["hooks"] if h["type"] == "http"]
    assert "headers" in fresh


def test_install_replaces_a_stale_octomate_url(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    for port in (1111, 2222):
        runner.invoke(
            claude_typer,
            [
                "hooks",
                "install",
                "--url",
                f"http://127.0.0.1:{port}/hooks/claude",
                "--settings",
                str(path),
            ],
        )
    stop = read(path)["hooks"]["Stop"]
    urls = {hook["url"] for group in stop for hook in group["hooks"]}
    assert urls == {"http://127.0.0.1:2222/hooks/claude"}  # only the fresh url remains


def test_install_adds_the_stream_launcher_on_prompt_submit_only(
    tmp_path: Path,
) -> None:
    """The transcript stream needs a local process, which only a `command` hook can
    start; it rides `UserPromptSubmit` — the event whose http hook has already created
    the session server-side by the time the tail connects — and no other event, since
    the tail deduplicates itself per session."""
    path = tmp_path / "settings.json"
    for _ in range(2):  # running twice must not duplicate the launcher either
        runner.invoke(
            claude_typer, ["hooks", "install", "--url", URL, "--settings", str(path)]
        )

    hooks = read(path)["hooks"]
    assert hook_types(hooks["UserPromptSubmit"]) == ["http", "command"]
    for event in ("Stop", "SessionEnd", "SubagentStart", "SubagentStop"):
        assert hook_types(hooks[event]) == ["http"]

    [launcher] = [
        hook
        for group in hooks["UserPromptSubmit"]
        for hook in group["hooks"]
        if hook["type"] == "command"
    ]
    # The command names this install's own interpreter and launch script by absolute
    # path, and points at the stream endpoint the hook URL implies.
    assert str(LAUNCH_SCRIPT) in launcher["command"]
    assert "ws://127.0.0.1:9999/hooks/claude/stream" in launcher["command"]


def test_install_replaces_a_launcher_left_by_an_older_install(tmp_path: Path) -> None:
    """Launchers are matched on the stream path they point at, not the launch script's
    own location, so an install replaces one whose script path a package rename or venv
    move has retired — instead of stacking a fresh launcher beside a broken one."""
    path = tmp_path / "settings.json"
    stale = {
        "type": "command",
        "command": "/old/venv/bin/python /old/packages/inklet/inklet/launch.py"
        " --url ws://127.0.0.1:9999/hooks/claude/stream --octomate /old/venv/bin/octomate",
    }
    path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [stale]}]}}))

    runner.invoke(
        claude_typer, ["hooks", "install", "--url", URL, "--settings", str(path)]
    )

    commands = [
        hook["command"]
        for group in read(path)["hooks"]["UserPromptSubmit"]
        for hook in group["hooks"]
        if hook["type"] == "command"
    ]
    assert stale["command"] not in commands
    assert sum(str(LAUNCH_SCRIPT) in command for command in commands) == 1


def test_no_launcher_skips_the_stream_and_retires_a_previous_one(
    tmp_path: Path,
) -> None:
    """The server's own machine skips the launcher — local sessions are tailed from
    disk, and a spawned tail would only be refused — and re-running with the flag
    also retires a launcher a previous install left."""
    path = tmp_path / "settings.json"
    runner.invoke(
        claude_typer, ["hooks", "install", "--url", URL, "--settings", str(path)]
    )
    runner.invoke(
        claude_typer,
        ["hooks", "install", "--url", URL, "--settings", str(path), "--no-launcher"],
    )

    assert hook_types(read(path)["hooks"]["UserPromptSubmit"]) == ["http"]


def test_uninstall_removes_the_launcher_too(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    runner.invoke(
        claude_typer, ["hooks", "install", "--url", URL, "--settings", str(path)]
    )
    runner.invoke(claude_typer, ["hooks", "uninstall", "--settings", str(path)])

    assert "hooks" not in read(path)


def test_hints_but_does_not_block_when_claude_is_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Agents:
        claude = None

    class Config:
        agents = Agents()
        hook_secret = None

    monkeypatch.setattr("octomate.config.OctomateConfig", Config)

    result = runner.invoke(
        claude_typer,
        ["hooks", "install", "--url", URL, "--settings", str(tmp_path / "s.json")],
    )
    assert result.exit_code == 0  # the command still runs
    assert "config.agents.claude" in result.output  # but hints that Claude is absent


def test_codex_install_runs_the_standalone_emit_script_on_this_interpreter(
    tmp_path: Path,
) -> None:
    """Not `-m octomate...`: importing the package costs ~1.9s on a hook Codex blocks
    on. By path, through `sys.executable` so it does not depend on the session's PATH."""
    path = tmp_path / "hooks.json"
    result = runner.invoke(
        codex_typer,
        ["hooks", "install", "--url", CODEX_URL, "--hooks-file", str(path)],
    )
    assert result.exit_code == 0

    handlers = [
        hook
        for groups in read(path)["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]
    assert handlers  # one per handled event
    for handler in handlers:
        assert str(EMIT_SCRIPT) in handler["command"]
        assert "-m octomate" not in handler["command"]


def test_codex_install_replaces_a_handler_left_by_an_older_version(
    tmp_path: Path,
) -> None:
    """Earlier versions invoked the CLI's `codex hooks emit`. Handlers are matched on the
    hook path, so upgrading replaces that one instead of stacking a second beside it."""
    path = tmp_path / "hooks.json"
    stale = {
        "type": "command",
        "command": f"/usr/bin/python -m octomate.cli.base codex hooks emit --url {CODEX_URL}",
    }
    path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [stale, COMMAND_HOOK]}]}}))

    runner.invoke(
        codex_typer,
        ["hooks", "install", "--url", CODEX_URL, "--hooks-file", str(path)],
    )

    commands = [
        hook["command"]
        for group in read(path)["hooks"]["Stop"]
        for hook in group["hooks"]
    ]
    assert stale["command"] not in commands  # the stale handler is gone, not duplicated
    assert "echo done" in commands  # an unrelated hook of the operator's survives
    assert sum(str(EMIT_SCRIPT) in command for command in commands) == 1


def configured(monkeypatch: pytest.MonkeyPatch, hook_secret: SecretStr | None) -> None:
    """Pin what Octomate can see, rather than reading the ambient config: whoever runs
    the suite has a hook secret of their own by now, and it must not decide these."""
    config = SimpleNamespace(hook_secret=hook_secret)
    monkeypatch.setattr("octomate.config.OctomateConfig", lambda: config)


def test_secret_hands_a_configured_credential_to_the_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Octomate may hold its secret in octomate.yaml, but a session only ever reads the
    environment — and it is a separate process that never sees that yaml. This is the
    bridge, so nobody copies a secret out of a config file by hand.

    Invoked through the root app, which is how it is really reached: a lone command in a
    sub-Typer is otherwise flattened away by Typer when invoked directly.
    """
    configured(monkeypatch, SecretStr("from-the-yaml"))

    result = runner.invoke(app, ["hooks", "secret"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "export OCTOMATE__HOOK_SECRET=from-the-yaml"


def test_a_configured_secret_is_never_rotated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-running is what someone does when hooks already work; handing back a new value
    would strand Octomate and every client still carrying the old one."""
    configured(monkeypatch, SecretStr("already-configured"))

    first = runner.invoke(app, ["hooks", "secret"])
    second = runner.invoke(app, ["hooks", "secret"])

    assert first.stdout == second.stdout
    assert "already-configured" in first.stdout


def test_secret_generates_one_when_none_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The line still comes out, so `eval` works the first time too — but the value
    exists nowhere yet, and only the operator can decide where it lives, so nothing is
    written and stderr says what is left to do."""
    monkeypatch.chdir(tmp_path)
    configured(monkeypatch, None)

    result = runner.invoke(app, ["hooks", "secret"])

    assert result.exit_code == 0
    assert result.stdout.startswith("export OCTOMATE__HOOK_SECRET=")
    assert list(tmp_path.iterdir()) == []  # nothing placed


def test_the_pretty_guidance_stays_out_of_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The panel is rendered to stderr on purpose. stdout is consumed by `eval` and
    `>>`, so a single stray glyph of decoration there would land in someone's shell
    profile — or be eval'd."""
    configured(monkeypatch, SecretStr("from-the-yaml"))

    result = runner.invoke(app, ["hooks", "secret"])

    assert result.stdout == "export OCTOMATE__HOOK_SECRET=from-the-yaml\n"
    assert "\x1b[" not in result.stdout  # nor any styling


def test_secret_writes_nothing_and_leaves_placing_to_the_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where an environment comes from is the operator's to know — not every shell is
    zsh, and a startup file is not this command's to edit. It hands over a line."""
    monkeypatch.chdir(tmp_path)
    configured(monkeypatch, SecretStr("from-the-yaml"))

    result = runner.invoke(app, ["hooks", "secret"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "export OCTOMATE__HOOK_SECRET=from-the-yaml"
    assert list(tmp_path.iterdir()) == []  # nothing written, anywhere


def test_a_redirected_line_survives_a_round_trip_through_the_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`octomate hooks secret >> ~/.zshenv` is the suggestion, so what matters is the
    value a shell reads back out of such a file — quoting and all."""
    configured(monkeypatch, SecretStr("has spaces & $dollars"))
    profile = tmp_path / ".zshenv"

    result = runner.invoke(app, ["hooks", "secret"])
    profile.write_text(result.stdout)  # what the operator's `>>` would put there

    sourced = subprocess.run(
        [f'. "{profile}"; printf %s "${{OCTOMATE__HOOK_SECRET}}"'],
        shell=True,
        capture_output=True,
        text=True,
    )
    assert sourced.stdout == "has spaces & $dollars"


def test_secret_quotes_a_credential_that_is_not_shell_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The line is made to be eval'd, and a hand-written secret need not be shell-safe."""
    configured(monkeypatch, SecretStr("has spaces & $dollars"))

    result = runner.invoke(app, ["hooks", "secret"])

    assert result.stdout.strip() == (
        "export OCTOMATE__HOOK_SECRET='has spaces & $dollars'"
    )
    # And eval'ing it really does reproduce the secret, quoting and all.
    assert (
        subprocess.run(
            [f'{result.stdout.strip()}; printf %s "${{OCTOMATE__HOOK_SECRET}}"'],
            shell=True,
            capture_output=True,
            text=True,
        ).stdout
        == "has spaces & $dollars"
    )


def test_installing_without_a_configured_secret_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hook config is half the pipe; an install that reports success while no secret
    exists would just 401 on the next turn."""

    class Agents:
        claude = None

    class Config:
        agents = Agents()
        hook_secret = None

    monkeypatch.setattr("octomate.config.OctomateConfig", Config)

    result = runner.invoke(
        claude_typer,
        ["hooks", "install", "--url", URL, "--settings", str(tmp_path / "s.json")],
    )

    assert result.exit_code == 0  # it still installs
    assert "octomate hooks secret" in result.output


def test_uninstall_removes_only_octomate_hooks(tmp_path: Path) -> None:
    path = settings_with(tmp_path, {"hooks": {"Stop": [{"hooks": [COMMAND_HOOK]}]}})
    runner.invoke(
        claude_typer, ["hooks", "install", "--url", URL, "--settings", str(path)]
    )

    result = runner.invoke(
        claude_typer, ["hooks", "uninstall", "--settings", str(path)]
    )
    assert result.exit_code == 0

    # Only the command hook remains; the events that held just Octomate's are dropped.
    assert read(path)["hooks"] == {"Stop": [{"hooks": [COMMAND_HOOK]}]}
