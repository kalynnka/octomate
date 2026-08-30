"""The operator CLI's hook install/uninstall merge behavior and client config."""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from octomate_cli import mcp as cli_mcp
from octomate_cli.claude import claude_typer
from octomate_cli.cli import app
from octomate_cli.codex import codex_typer
from octomate_cli.config import resolved_secret, resolved_url, user_config_path
from octomate_cli.deepseek import deepseek_typer
from octomate_cli.hooks import EMIT_SCRIPT, LAUNCH_SCRIPT
from typer.testing import CliRunner

from octomate.config.base import OctomateConfig
from octomate.mcp import gateway as served_gateway
from octomate.types import threads

runner = CliRunner()
URL = "http://127.0.0.1:9999/hooks/claude"
CODEX_URL = "http://127.0.0.1:9999/hooks/codex"
COMMAND_HOOK = {"type": "command", "command": "echo done"}


@pytest.fixture(autouse=True)
def isolated_client_config(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point the client config at an empty per-test home and cwd: the suite must
    never read — or be steered by — the developer's real cli.toml in either scope."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))


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
    # The pre-existing command hook survives beside one Octomate emit handler.
    assert hook_types(hooks["Stop"]) == ["command", "command"]
    [emit] = [
        h for g in hooks["Stop"] for h in g["hooks"] if str(EMIT_SCRIPT) in h["command"]
    ]
    assert "--path /hooks/claude" in emit["command"]
    # No SessionStart: the server acts on nothing in it — the first prompt starts the
    # session — so registering it would spawn a process per session for nothing.
    assert set(hooks) == {
        "UserPromptSubmit",
        "Stop",
        "SessionEnd",
        "SubagentStart",
        "SubagentStop",
    }


def test_the_installed_handler_carries_neither_credential_nor_host(
    tmp_path: Path,
) -> None:
    """A settings file is a document people commit and share; the credential and,
    without a pinned `--url`, the server's address are both environment — the emit
    script resolves them when each hook fires, so the same install serves whichever
    server `OCTOMATE_URL` names."""
    path = tmp_path / "settings.json"
    runner.invoke(claude_typer, ["hooks", "install", "--settings", str(path)])

    [handler] = [
        hook for group in read(path)["hooks"]["Stop"] for hook in group["hooks"]
    ]
    assert handler["type"] == "command"
    assert "--path /hooks/claude" in handler["command"]
    assert "--url" not in handler["command"]
    assert "OCTOMATE__SECRET" not in json.dumps(read(path))


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
    assert hook_types(hooks["Stop"]) == ["command", "command"]
    [fresh] = [
        h for g in hooks["Stop"] for h in g["hooks"] if str(EMIT_SCRIPT) in h["command"]
    ]
    assert "--path /hooks/claude" in fresh["command"]


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
    commands = [hook["command"] for group in stop for hook in group["hooks"]]
    assert len(commands) == 1  # only the fresh pin remains, not one handler per port
    assert "http://127.0.0.1:2222/hooks/claude" in commands[0]
    assert "1111" not in commands[0]


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
    assert hook_types(hooks["UserPromptSubmit"]) == ["command", "command"]
    for event in ("Stop", "SessionEnd", "SubagentStart", "SubagentStop"):
        assert hook_types(hooks[event]) == ["command"]

    [launcher] = [
        hook
        for group in hooks["UserPromptSubmit"]
        for hook in group["hooks"]
        if str(LAUNCH_SCRIPT) in hook["command"]
    ]
    # The command names this install's own interpreter and launch script by absolute
    # path, and — the install pinned --url — the stream endpoint the hook URL implies.
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


def test_uninstall_removes_the_launcher_too(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    runner.invoke(
        claude_typer, ["hooks", "install", "--url", URL, "--settings", str(path)]
    )
    runner.invoke(claude_typer, ["hooks", "uninstall", "--settings", str(path)])

    assert "hooks" not in read(path)


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
    assert handlers  # an emit per handled event, a launcher per session event
    for handler in handlers:
        assert str(EMIT_SCRIPT) in handler["command"] or (
            str(LAUNCH_SCRIPT) in handler["command"]
        )
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


def test_codex_install_adds_the_launcher_on_the_session_events(tmp_path: Path) -> None:
    """The tail spawns from the session events; `SubagentStop` also carries the
    launcher because its `agent_transcript_path` is how a child rollout's path
    reaches the running tail. `Stop` and `SubagentStart` name no file the tail needs,
    so they carry only the emit hook."""
    path = tmp_path / "hooks.json"
    for _ in range(2):  # running twice must not duplicate the launcher either
        runner.invoke(
            codex_typer,
            ["hooks", "install", "--url", CODEX_URL, "--hooks-file", str(path)],
        )

    hooks = read(path)["hooks"]
    for event in ("SessionStart", "UserPromptSubmit", "SubagentStop"):
        assert hook_types(hooks[event]) == ["command", "command"]
    for event in ("Stop", "SubagentStart"):
        assert hook_types(hooks[event]) == ["command"]

    [launcher] = [
        hook
        for group in hooks["SubagentStop"]
        for hook in group["hooks"]
        if str(LAUNCH_SCRIPT) in hook["command"]
    ]
    assert "--agent codex" in launcher["command"]
    assert "ws://127.0.0.1:9999/hooks/codex/stream" in launcher["command"]


def test_configure_writes_the_client_file_with_tight_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The durable floor under the environment: two keys in one file, readable only by
    its owner, since one of them is a credential."""
    configured(monkeypatch, None)
    result = runner.invoke(
        app,
        ["configure", "--url", "http://minidock.local:8000", "--secret", "s3cr3t"],
    )

    assert result.exit_code == 0
    path = user_config_path()
    assert tomllib.loads(path.read_text()) == {
        "url": "http://minidock.local:8000",
        "secret": "s3cr3t",
    }
    assert path.stat().st_mode & 0o777 == 0o600


def test_configure_generates_a_secret_when_nothing_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured(monkeypatch, None)
    result = runner.invoke(app, ["configure", "--url", "http://minidock.local:8000"])

    assert result.exit_code == 0
    assert tomllib.loads(user_config_path().read_text())["secret"]
    assert "generated" in result.output


def test_configure_carries_an_env_secret_into_the_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An environment dies with its shell, and hooks fire from launch paths that never
    sourced one; configure copies what resolves into the durable home instead of
    minting a second credential beside it."""
    configured(monkeypatch, "from-the-shell")
    runner.invoke(app, ["configure", "--url", "http://minidock.local:8000"])

    assert tomllib.loads(user_config_path().read_text())["secret"] == "from-the-shell"


def test_the_environment_beats_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """The file is the floor: an env switch (the debug-server case) must win, and the
    file must still fill whatever the environment leaves unset."""
    configured(monkeypatch, None)
    runner.invoke(
        app, ["configure", "--url", "http://file:1", "--secret", "file-secret"]
    )
    monkeypatch.setenv("OCTOMATE_URL", "http://env:2")

    assert resolved_url() == "http://env:2"
    assert resolved_secret() == "file-secret"


def test_configure_project_scope_writes_beside_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--scope project` writes `./.octomate/cli.toml` — the way `.claude/settings.json`
    scopes Claude — so one directory can aim at its own server."""
    configured(monkeypatch, None)
    result = runner.invoke(
        app,
        ["configure", "--scope", "project", "--url", "http://debug:1", "--secret", "s"],
    )

    assert result.exit_code == 0
    table = tomllib.loads((Path.cwd() / ".octomate" / "cli.toml").read_text())
    assert table["url"] == "http://debug:1"


def test_the_project_file_beats_the_user_file_per_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scopes merge per key, like Claude's settings: the project pins its url while
    the secret still comes from the user file."""
    configured(monkeypatch, None)
    runner.invoke(
        app, ["configure", "--url", "http://user:1", "--secret", "user-secret"]
    )
    (Path.cwd() / ".octomate").mkdir()
    (Path.cwd() / ".octomate" / "cli.toml").write_text('url = "http://project:2"\n')

    assert resolved_url() == "http://project:2"
    assert resolved_secret() == "user-secret"


def test_codex_install_without_url_leaves_the_target_to_the_environment(
    tmp_path: Path,
) -> None:
    """No pinned host: the emit command carries only the hook path, and the script
    resolves $OCTOMATE_URL when each hook fires — switching servers is an environment
    switch, not a re-install."""
    path = tmp_path / "hooks.json"
    result = runner.invoke(codex_typer, ["hooks", "install", "--hooks-file", str(path)])
    assert result.exit_code == 0

    handlers = [
        hook
        for groups in read(path)["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]
    assert handlers
    for handler in handlers:
        assert "--path /hooks/codex" in handler["command"]
        assert "--url" not in handler["command"]


def configured(monkeypatch: pytest.MonkeyPatch, secret: str | None) -> None:
    """Pin what the client resolves, rather than reading the ambient environment:
    whoever runs the suite has a secret of their own by now, and it must not
    decide these. (The config-file source is already isolated per test.)"""
    monkeypatch.delenv("OCTOMATE__SECRET", raising=False)
    if secret is not None:
        monkeypatch.setenv("OCTOMATE__SECRET", secret)


def test_secret_hands_a_configured_credential_to_the_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prints whatever the client already resolves — here the environment — as an
    eval'able line, so nobody copies a secret around by hand.
    """
    configured(monkeypatch, "from-the-env")

    result = runner.invoke(app, ["secret"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "export OCTOMATE__SECRET=from-the-env"


def test_a_configured_secret_is_never_rotated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-running is what someone does when hooks already work; handing back a new value
    would strand Octomate and every client still carrying the old one."""
    configured(monkeypatch, "already-configured")

    first = runner.invoke(app, ["secret"])
    second = runner.invoke(app, ["secret"])

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

    result = runner.invoke(app, ["secret"])

    assert result.exit_code == 0
    assert result.stdout.startswith("export OCTOMATE__SECRET=")
    assert list(tmp_path.iterdir()) == []  # nothing placed


def test_the_pretty_guidance_stays_out_of_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The panel is rendered to stderr on purpose. stdout is consumed by `eval` and
    `>>`, so a single stray glyph of decoration there would land in someone's shell
    profile — or be eval'd."""
    configured(monkeypatch, "from-the-env")

    result = runner.invoke(app, ["secret"])

    assert result.stdout == "export OCTOMATE__SECRET=from-the-env\n"
    assert "\x1b[" not in result.stdout  # nor any styling


def test_secret_writes_nothing_and_leaves_placing_to_the_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where an environment comes from is the operator's to know — not every shell is
    zsh, and a startup file is not this command's to edit. It hands over a line."""
    monkeypatch.chdir(tmp_path)
    configured(monkeypatch, "from-the-env")

    result = runner.invoke(app, ["secret"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "export OCTOMATE__SECRET=from-the-env"
    assert list(tmp_path.iterdir()) == []  # nothing written, anywhere


def test_a_redirected_line_survives_a_round_trip_through_the_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`octomate secret >> ~/.zshenv` is the suggestion, so what matters is the
    value a shell reads back out of such a file — quoting and all."""
    configured(monkeypatch, "has spaces & $dollars")
    profile = tmp_path / ".zshenv"

    result = runner.invoke(app, ["secret"])
    profile.write_text(result.stdout)  # what the operator's `>>` would put there

    sourced = subprocess.run(
        [f'. "{profile}"; printf %s "${{OCTOMATE__SECRET}}"'],
        shell=True,
        capture_output=True,
        text=True,
    )
    assert sourced.stdout == "has spaces & $dollars"


def test_secret_quotes_a_credential_that_is_not_shell_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The line is made to be eval'd, and a hand-written secret need not be shell-safe."""
    configured(monkeypatch, "has spaces & $dollars")

    result = runner.invoke(app, ["secret"])

    assert result.stdout.strip() == ("export OCTOMATE__SECRET='has spaces & $dollars'")
    # And eval'ing it really does reproduce the secret, quoting and all.
    assert (
        subprocess.run(
            [f'{result.stdout.strip()}; printf %s "${{OCTOMATE__SECRET}}"'],
            shell=True,
            capture_output=True,
            text=True,
        ).stdout
        == "has spaces & $dollars"
    )


def test_installing_without_a_resolving_secret_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hook config is half the pipe; an install that reports success while no
    credential resolves would just 401 on the next turn."""
    configured(monkeypatch, None)

    result = runner.invoke(
        claude_typer,
        ["hooks", "install", "--url", URL, "--settings", str(tmp_path / "s.json")],
    )

    assert result.exit_code == 0  # it still installs
    assert "octomate configure" in result.output


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


def test_deepseek_install_writes_the_hooks_file_and_patch_row(tmp_path: Path) -> None:
    result = runner.invoke(
        deepseek_typer, ["hooks", "install", "--home", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    document = read(tmp_path / "octomate-hooks.json")
    assert sorted(document["hooks"]) == ["Stop", "UserPromptSubmit"]
    for groups in document["hooks"].values():
        for group in groups:
            [handler] = group["hooks"]
            assert handler["type"] == "command"
            assert "/hooks/deepseek" in handler["command"]
            assert "--url" not in handler["command"]
            assert handler["timeout"] == 10
    # The stream launcher rides the prompt event only; Stop keeps just the emit.
    assert hook_types(document["hooks"]["UserPromptSubmit"]) == ["command", "command"]
    assert hook_types(document["hooks"]["Stop"]) == ["command"]
    [launcher] = [
        hook
        for group in document["hooks"]["UserPromptSubmit"]
        for hook in group["hooks"]
        if str(LAUNCH_SCRIPT) in hook["command"]
    ]
    assert "--agent deepseek" in launcher["command"]
    [stop_group] = document["hooks"]["Stop"]
    assert str(EMIT_SCRIPT) in stop_group["hooks"][0]["command"]

    patch = (tmp_path / "cordis.patch.yml").read_text()
    assert "octomate-hooks" in patch
    assert "@deepseek-ai/dsh-hooks-claude-code" in patch
    assert str(tmp_path / "octomate-hooks.json") in patch


def test_deepseek_install_pins_the_stream_url_only_with_a_pinned_hook_url(
    tmp_path: Path,
) -> None:
    runner.invoke(
        deepseek_typer,
        [
            "hooks",
            "install",
            "--home",
            str(tmp_path),
            "--url",
            "http://127.0.0.1:9999/hooks/deepseek",
        ],
    )

    document = read(tmp_path / "octomate-hooks.json")
    [launcher] = [
        hook
        for group in document["hooks"]["UserPromptSubmit"]
        for hook in group["hooks"]
        if str(LAUNCH_SCRIPT) in hook["command"]
    ]
    assert "ws://127.0.0.1:9999/hooks/deepseek/stream" in launcher["command"]


def test_deepseek_install_replaces_the_default_empty_patch_document(
    tmp_path: Path,
) -> None:
    (tmp_path / "cordis.patch.yml").write_text(
        "# Your patch layer for this dsh profile.\n[]\n"
    )
    runner.invoke(deepseek_typer, ["hooks", "install", "--home", str(tmp_path)])

    text = (tmp_path / "cordis.patch.yml").read_text()
    assert "# Your patch layer" in text
    assert text.count("- insert:") == 1
    # The lone `[]` was the whole document; appending after it would be
    # invalid YAML, so the install replaces it with the block.
    assert "[]" not in text


def test_deepseek_install_is_idempotent_and_keeps_the_operators_rows(
    tmp_path: Path,
) -> None:
    (tmp_path / "cordis.patch.yml").write_text(
        "# machine prefs\n- id: session-query-sqlite\n  disabled: true\n"
    )
    runner.invoke(deepseek_typer, ["hooks", "install", "--home", str(tmp_path)])
    runner.invoke(
        deepseek_typer,
        ["hooks", "install", "--home", str(tmp_path), "--url", URL],
    )

    text = (tmp_path / "cordis.patch.yml").read_text()
    assert text.count("- insert:") == 1
    assert "session-query-sqlite" in text
    assert "# machine prefs" in text


def test_deepseek_uninstall_restores_an_empty_document(tmp_path: Path) -> None:
    runner.invoke(deepseek_typer, ["hooks", "install", "--home", str(tmp_path)])
    result = runner.invoke(
        deepseek_typer, ["hooks", "uninstall", "--home", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "octomate-hooks.json").exists()
    text = (tmp_path / "cordis.patch.yml").read_text()
    assert "octomate-hooks" not in text
    assert text.strip().splitlines()[-1] == "[]"


def test_deepseek_uninstall_keeps_the_operators_rows(tmp_path: Path) -> None:
    (tmp_path / "cordis.patch.yml").write_text(
        "- id: session-query-sqlite\n  disabled: true\n"
    )
    runner.invoke(deepseek_typer, ["hooks", "install", "--home", str(tmp_path)])
    runner.invoke(deepseek_typer, ["hooks", "uninstall", "--home", str(tmp_path)])

    text = (tmp_path / "cordis.patch.yml").read_text()
    assert "session-query-sqlite" in text
    assert "octomate-hooks" not in text
    assert "[]" not in text


def test_deepseek_install_links_a_valid_bridge_package(tmp_path: Path) -> None:
    bridge = tmp_path / "checkout" / "hooks-claude-code"
    (bridge / "lib").mkdir(parents=True)
    (bridge / "lib" / "index.js").write_text("export {}\n")
    (bridge / "package.json").write_text(
        json.dumps({"name": "@deepseek-ai/dsh-hooks-claude-code"})
    )
    home = tmp_path / "dsh-home"

    result = runner.invoke(
        deepseek_typer,
        ["hooks", "install", "--home", str(home), "--bridge", str(bridge)],
    )

    assert result.exit_code == 0, result.output
    link = home / "profiles" / "node_modules" / "@deepseek-ai" / "dsh-hooks-claude-code"
    assert link.is_symlink()
    assert link.readlink() == bridge.resolve()


def test_deepseek_install_refuses_a_wrong_bridge_package(tmp_path: Path) -> None:
    bridge = tmp_path / "somewhere-else"
    (bridge / "lib").mkdir(parents=True)
    (bridge / "lib" / "index.js").write_text("export {}\n")
    (bridge / "package.json").write_text(json.dumps({"name": "@deepseek-ai/dsh-agent"}))

    result = runner.invoke(
        deepseek_typer,
        ["hooks", "install", "--home", str(tmp_path / "home"), "--bridge", str(bridge)],
    )

    assert result.exit_code != 0
    assert "dsh-hooks-claude-code" in result.output


def test_deepseek_install_without_a_bridge_link_warns(tmp_path: Path) -> None:
    result = runner.invoke(
        deepseek_typer, ["hooks", "install", "--home", str(tmp_path)]
    )

    assert result.exit_code == 0
    assert "--bridge" in result.output


def gateway_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """An address and a credential resolving from the environment — the way a
    machine that ran `octomate configure` looks to an installer."""
    monkeypatch.setenv("OCTOMATE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("OCTOMATE__SECRET", "the-secret")


def test_claude_mcp_install_writes_the_entry_beside_everything_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway_ready(monkeypatch)
    path = tmp_path / "claude.json"
    path.write_text(
        json.dumps({"model": "opus", "mcpServers": {"other": {"type": "stdio"}}})
    )

    result = runner.invoke(
        claude_typer, ["mcp", "install", "--scope", "user", "--file", str(path)]
    )
    assert result.exit_code == 0, result.output

    document = read(path)
    assert document["model"] == "opus"
    assert document["mcpServers"]["other"] == {"type": "stdio"}
    assert document["mcpServers"]["gateway"] == {
        "type": "http",
        "url": "http://127.0.0.1:9999/gateway/mcp",
        "headers": {
            "Authorization": "Bearer the-secret",
            "X-Octomate-Client": "claude-native",
        },
    }


def test_claude_mcp_reinstall_replaces_the_entry_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway_ready(monkeypatch)
    path = tmp_path / "claude.json"
    runner.invoke(
        claude_typer, ["mcp", "install", "--scope", "user", "--file", str(path)]
    )
    runner.invoke(
        claude_typer,
        [
            "mcp",
            "install",
            "--url",
            "http://minidock.local:8000",
            "--scope",
            "user",
            "--file",
            str(path),
        ],
    )

    servers = read(path)["mcpServers"]
    assert list(servers) == ["gateway"]
    assert servers["gateway"]["url"] == "http://minidock.local:8000/gateway/mcp"


def test_claude_mcp_uninstall_keeps_foreign_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway_ready(monkeypatch)
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"type": "stdio"}}}))
    runner.invoke(
        claude_typer, ["mcp", "install", "--scope", "user", "--file", str(path)]
    )

    result = runner.invoke(
        claude_typer, ["mcp", "uninstall", "--scope", "user", "--file", str(path)]
    )
    assert result.exit_code == 0

    assert read(path)["mcpServers"] == {"other": {"type": "stdio"}}


def test_claude_mcp_uninstall_drops_an_emptied_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway_ready(monkeypatch)
    path = tmp_path / "claude.json"
    runner.invoke(
        claude_typer, ["mcp", "install", "--scope", "user", "--file", str(path)]
    )
    runner.invoke(
        claude_typer, ["mcp", "uninstall", "--scope", "user", "--file", str(path)]
    )

    assert "mcpServers" not in read(path)


def test_claude_mcp_show_masks_the_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway_ready(monkeypatch)
    path = tmp_path / "claude.json"
    runner.invoke(
        claude_typer, ["mcp", "install", "--scope", "user", "--file", str(path)]
    )

    result = runner.invoke(
        claude_typer, ["mcp", "show", "--scope", "user", "--file", str(path)]
    )

    assert result.exit_code == 0
    assert "http://127.0.0.1:9999/gateway/mcp" in result.output
    assert "Bearer ***" in result.output
    assert "the-secret" not in result.output


def test_claude_mcp_local_install_nests_under_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway_ready(monkeypatch)
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "claude.json"
    path.write_text(
        json.dumps(
            {
                "model": "opus",
                "projects": {
                    "/elsewhere": {"mcpServers": {"other": {"type": "stdio"}}}
                },
            }
        )
    )

    result = runner.invoke(claude_typer, ["mcp", "install", "--file", str(path)])
    assert result.exit_code == 0, result.output

    document = read(path)
    assert document["model"] == "opus"
    assert "mcpServers" not in document
    assert document["projects"]["/elsewhere"] == {
        "mcpServers": {"other": {"type": "stdio"}}
    }
    assert document["projects"][str(Path.cwd())]["mcpServers"]["gateway"] == {
        "type": "http",
        "url": "http://127.0.0.1:9999/gateway/mcp",
        "headers": {
            "Authorization": "Bearer the-secret",
            "X-Octomate-Client": "claude-native",
        },
    }


def test_claude_mcp_local_uninstall_prunes_what_install_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway_ready(monkeypatch)
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "claude.json"
    runner.invoke(claude_typer, ["mcp", "install", "--file", str(path)])

    result = runner.invoke(claude_typer, ["mcp", "uninstall", "--file", str(path)])
    assert result.exit_code == 0

    assert read(path) == {}


def test_claude_mcp_local_uninstall_keeps_the_projects_other_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway_ready(monkeypatch)
    monkeypatch.chdir(tmp_path)
    key = str(Path.cwd())
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({"projects": {key: {"history": ["a prompt"]}}}))
    runner.invoke(claude_typer, ["mcp", "install", "--file", str(path)])

    runner.invoke(claude_typer, ["mcp", "uninstall", "--file", str(path)])

    assert read(path) == {"projects": {key: {"history": ["a prompt"]}}}


def test_claude_mcp_show_reads_the_local_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway_ready(monkeypatch)
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "claude.json"
    runner.invoke(claude_typer, ["mcp", "install", "--file", str(path)])

    result = runner.invoke(claude_typer, ["mcp", "show", "--file", str(path)])

    assert result.exit_code == 0
    assert "http://127.0.0.1:9999/gateway/mcp" in result.output
    assert "the-secret" not in result.output


def test_mcp_install_refuses_without_an_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A static entry pointing nowhere would fail every session's tool listing,
    so — unlike the hooks, which resolve at fire time — the install refuses."""
    monkeypatch.delenv("OCTOMATE_URL", raising=False)
    monkeypatch.setenv("OCTOMATE__SECRET", "the-secret")
    invocations = [
        (claude_typer, ["mcp", "install", "--file", str(tmp_path / "c.json")]),
        (codex_typer, ["mcp", "install", "--config-file", str(tmp_path / "c.toml")]),
        (deepseek_typer, ["mcp", "install", "--home", str(tmp_path)]),
    ]
    for typer_app, args in invocations:
        result = runner.invoke(typer_app, args)
        assert result.exit_code != 0
        assert "needs a concrete address" in result.output
    assert list(tmp_path.iterdir()) == []  # nothing half-written


def test_mcp_install_refuses_without_the_credential_it_would_embed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude's and dsh's entries hold the literal credential; without one the
    written entry would only 401, so the install refuses instead."""
    monkeypatch.setenv("OCTOMATE_URL", "http://127.0.0.1:9999")
    monkeypatch.delenv("OCTOMATE__SECRET", raising=False)
    invocations = [
        (claude_typer, ["mcp", "install", "--file", str(tmp_path / "c.json")]),
        (deepseek_typer, ["mcp", "install", "--home", str(tmp_path)]),
    ]
    for typer_app, args in invocations:
        result = runner.invoke(typer_app, args)
        assert result.exit_code != 0
        assert "no credential resolves" in result.output
    assert list(tmp_path.iterdir()) == []


def test_codex_mcp_install_needs_no_credential_but_says_where_it_lives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex's entry names the environment variable rather than embedding the
    value, so a missing credential gets the hooks' warning, not a refusal."""
    monkeypatch.setenv("OCTOMATE_URL", "http://127.0.0.1:9999")
    monkeypatch.delenv("OCTOMATE__SECRET", raising=False)
    path = tmp_path / "config.toml"

    result = runner.invoke(codex_typer, ["mcp", "install", "--config-file", str(path)])

    assert result.exit_code == 0, result.output
    assert "octomate configure" in result.output  # the missing-credential warning
    table = tomllib.loads(path.read_text())
    assert table["mcp_servers"]["gateway"]["bearer_token_env_var"] == "OCTOMATE__SECRET"


def test_codex_mcp_install_preserves_comments_and_foreign_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway_ready(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text(
        '# the operator wrote this\nmodel = "gpt-5.5"\n\n'
        '[mcp_servers.logfire]\nurl = "https://logfire.dev/mcp"\n'
    )

    result = runner.invoke(codex_typer, ["mcp", "install", "--config-file", str(path)])
    assert result.exit_code == 0, result.output

    text = path.read_text()
    assert "# the operator wrote this" in text
    assert "the-secret" not in text  # the credential never lands in this file
    table = tomllib.loads(text)
    assert table["model"] == "gpt-5.5"
    assert table["mcp_servers"]["logfire"] == {"url": "https://logfire.dev/mcp"}
    assert table["mcp_servers"]["gateway"] == {
        "url": "http://127.0.0.1:9999/gateway/mcp",
        "bearer_token_env_var": "OCTOMATE__SECRET",
        "http_headers": {"X-Octomate-Client": "codex-native"},
    }


def test_codex_mcp_reinstall_and_uninstall_leave_the_operators_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway_ready(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text('# machine prefs\n[mcp_servers.logfire]\nurl = "https://l/mcp"\n')
    runner.invoke(codex_typer, ["mcp", "install", "--config-file", str(path)])
    runner.invoke(
        codex_typer,
        [
            "mcp",
            "install",
            "--url",
            "http://minidock.local:8000",
            "--config-file",
            str(path),
        ],
    )

    table = tomllib.loads(path.read_text())
    assert (
        table["mcp_servers"]["gateway"]["url"]
        == "http://minidock.local:8000/gateway/mcp"
    )

    result = runner.invoke(
        codex_typer, ["mcp", "uninstall", "--config-file", str(path)]
    )
    assert result.exit_code == 0
    text = path.read_text()
    assert "# machine prefs" in text
    table = tomllib.loads(text)
    assert "gateway" not in table["mcp_servers"]
    assert table["mcp_servers"]["logfire"] == {"url": "https://l/mcp"}


def test_codex_mcp_uninstall_drops_an_emptied_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway_ready(monkeypatch)
    path = tmp_path / "config.toml"
    runner.invoke(codex_typer, ["mcp", "install", "--config-file", str(path)])
    runner.invoke(codex_typer, ["mcp", "uninstall", "--config-file", str(path)])

    assert tomllib.loads(path.read_text()) == {}


def test_deepseek_mcp_install_writes_the_gateway_row_beside_the_hooks_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway_ready(monkeypatch)
    runner.invoke(deepseek_typer, ["hooks", "install", "--home", str(tmp_path)])
    for _ in range(2):  # running twice must not duplicate the row
        result = runner.invoke(
            deepseek_typer, ["mcp", "install", "--home", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output

    text = (tmp_path / "cordis.patch.yml").read_text()
    assert "octomate-hooks" in text  # the hooks row survives beside the new one
    assert text.count("serverName: gateway") == 1
    rows = yaml.safe_load(text)
    [gateway_row] = [
        row["insert"][0] for row in rows if row["insert"][0]["id"] == "octomate-gateway"
    ]
    assert gateway_row["name"] == "@deepseek-ai/dsh-mcp-client"
    assert gateway_row["config"] == {
        "serverName": "gateway",
        "transport": "streamable-http",
        "url": "http://127.0.0.1:9999/gateway/mcp",
        "headers": {
            "Authorization": "Bearer the-secret",
            "X-Octomate-Client": "deepseek-native",
        },
    }


def test_deepseek_mcp_and_hooks_blocks_come_and_go_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway_ready(monkeypatch)
    runner.invoke(deepseek_typer, ["hooks", "install", "--home", str(tmp_path)])
    runner.invoke(deepseek_typer, ["mcp", "install", "--home", str(tmp_path)])
    runner.invoke(deepseek_typer, ["hooks", "uninstall", "--home", str(tmp_path)])

    text = (tmp_path / "cordis.patch.yml").read_text()
    assert "octomate-gateway" in text  # the gateway row outlives the hooks row
    assert "octomate-hooks" not in text

    runner.invoke(deepseek_typer, ["mcp", "uninstall", "--home", str(tmp_path)])
    text = (tmp_path / "cordis.patch.yml").read_text()
    assert "octomate-gateway" not in text
    assert text.strip().splitlines()[-1] == "[]"  # the empty document restored


def test_deepseek_mcp_show_masks_the_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway_ready(monkeypatch)
    runner.invoke(deepseek_typer, ["mcp", "install", "--home", str(tmp_path)])

    result = runner.invoke(deepseek_typer, ["mcp", "show", "--home", str(tmp_path)])

    assert result.exit_code == 0
    assert "serverName: gateway" in result.output
    assert "Bearer ***" in result.output
    assert "the-secret" not in result.output


def test_the_cli_gateway_literals_match_the_servers() -> None:
    """The CLI cannot import the server package, so its `/gateway/mcp`, header,
    and client-id literals are copies — this test is what holds them together."""
    default_mcp_path = OctomateConfig.model_fields["mcp_path"].default
    assert cli_mcp.GATEWAY_MCP_PATH == (
        f"/{served_gateway.GATEWAY_SERVER_NAME}{default_mcp_path}"
    )
    assert cli_mcp.GATEWAY_SERVER_KEY == served_gateway.GATEWAY_SERVER_NAME
    assert cli_mcp.CLIENT_HEADER == served_gateway.CLIENT_HEADER
    assert cli_mcp.CLAUDE_NATIVE_CLIENT == threads.CLAUDE_NATIVE_ID
    assert cli_mcp.CODEX_NATIVE_CLIENT == threads.CODEX_NATIVE_ID
    assert cli_mcp.DEEPSEEK_NATIVE_CLIENT == threads.DEEPSEEK_NATIVE_ID
