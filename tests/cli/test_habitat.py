"""Habitat setup reviews bindings without collecting or copying credentials."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import typer
import yaml
from octomate_cli import habitat
from octomate_cli.habitat import HabitatMode
from octomate_cli.installation import DeploymentTarget
from octomate_cli.main import app
from octomate_cli.wizard.habitat import habitat_step
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def sbx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Mock:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(habitat.sys, "platform", "darwin")
    monkeypatch.setattr(habitat.os, "getuid", lambda: 501)
    monkeypatch.setattr(habitat.shutil, "which", lambda name: f"/bin/{name}")

    def command(executable: str, *arguments: str, capture: bool = False) -> str:
        if arguments == ("version",):
            return "sbx version v0.48.0 test"
        if arguments[:2] == ("settings", "get"):
            return "false\n"
        if arguments[:2] == ("secret", "ls"):
            return json.dumps(
                {
                    "secrets": [
                        {
                            "scope": "global",
                            "type": "service",
                            "name": service,
                            "secret": "(oauth configured)",
                        }
                        for service in ("openai", "anthropic")
                    ]
                }
            )
        return ""

    command_mock = Mock(side_effect=command)
    monkeypatch.setattr(habitat, "run_sbx", command_mock)
    return command_mock


def test_setup_merges_under_the_correct_provider_and_preserves_other_settings(
    sbx: Mock,
    tmp_path: Path,
) -> None:
    path = tmp_path / ".config/sbx/credentials.yaml"
    path.parent.mkdir(parents=True)
    original = {
        "custom": None,
        "bindings": {
            "anthropic": {"oauth": {"domains": ["chatgpt.com"], "custom": "keep"}}
        },
    }
    path.write_text(yaml.safe_dump(original))
    result = runner.invoke(
        app, ["habitat", "setup", "sbx", "--agent", "codex"], input="n\nkeep\ny\n"
    )
    assert result.exit_code == 0, result.output
    document = yaml.safe_load(path.read_text())
    assert document["custom"] is None
    assert document["bindings"]["anthropic"] == original["bindings"]["anthropic"]
    assert document["bindings"]["openai"] == {
        "oauth": {"domains": ["auth.openai.com", "chatgpt.com"]}
    }
    assert "auth.openai.com" in result.output
    assert not any("set" in call.args for call in sbx.call_args_list)
    before = path.read_bytes()
    checked = runner.invoke(
        app, ["habitat", "setup", "sbx", "--agent", "codex", "--check"]
    )
    assert checked.exit_code == 0, checked.output
    repeated = runner.invoke(
        app, ["habitat", "setup", "sbx", "--agent", "codex"], input="n\nkeep\n"
    )
    assert repeated.exit_code == 0, repeated.output
    assert path.read_bytes() == before


def test_login_runs_in_sbx_then_explicit_approval_creates_binding(
    sbx: Mock, tmp_path: Path
) -> None:
    result = runner.invoke(
        app, ["habitat", "setup", "sbx", "--agent", "codex"], input="y\noauth\ny\n"
    )
    assert result.exit_code == 0, result.output
    calls = [call.args for call in sbx.call_args_list]
    assert ("/bin/sbx", "login") in calls
    assert ("/bin/sbx", "secret", "set", "openai", "--oauth") in calls
    assert yaml.safe_load((tmp_path / ".config/sbx/credentials.yaml").read_text()) == {
        "bindings": {
            "openai": {"oauth": {"domains": ["auth.openai.com", "chatgpt.com"]}}
        }
    }


@pytest.mark.parametrize("check", [False, True])
def test_declined_or_missing_approval_leaves_no_file(
    sbx: Mock, tmp_path: Path, check: bool
) -> None:
    result = runner.invoke(
        app,
        [
            "habitat",
            "setup",
            "sbx",
            "--agent",
            "codex",
            *(["--check"] if check else []),
        ],
        input="" if check else "n\nkeep\nn\n",
    )
    assert result.exit_code != 0
    assert not (tmp_path / ".config/sbx/credentials.yaml").exists()


def test_malformed_binding_is_not_replaced_or_followed_by_login(
    sbx: Mock, tmp_path: Path
) -> None:
    path = tmp_path / ".config/sbx/credentials.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("bindings: []\n")
    result = runner.invoke(app, ["habitat", "setup", "sbx", "--agent", "codex"])
    assert result.exit_code != 0
    assert "Cannot read sbx bindings" in result.output
    assert path.read_text() == "bindings: []\n"
    assert [call.args for call in sbx.call_args_list] == [("/bin/sbx", "version")]


def test_claude_reuses_its_existing_oauth_login(sbx: Mock, tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["habitat", "setup", "sbx", "--agent", "claude"], input="n\ny\n"
    )
    assert result.exit_code == 0, result.output
    assert "existing sbx OAuth" in result.output
    assert not any("set" in call.args for call in sbx.call_args_list)


def test_domains_match_the_shipped_kit() -> None:
    kit = yaml.safe_load(
        (Path(__file__).parents[2] / "octomate/habitat/spec.yaml").read_text()
    )
    expected = {}
    for credential in kit["credentials"]:
        modes = {}
        if "oauth" in credential:
            oauth = credential["oauth"]
            modes["oauth"] = [oauth["tokenEndpoint"]["host"], *oauth["resourceHosts"]]
        if "apiKey" in credential:
            modes["apiKey"] = [
                entry["domain"] for entry in credential["apiKey"]["inject"]
            ]
        expected[credential["service"]] = modes
    assert habitat.BINDING_DOMAINS == expected


@pytest.mark.parametrize(
    ("agents", "target"),
    [(["deepseek"], DeploymentTarget.launchd), (["codex"], DeploymentTarget.docker)],
)
def test_sbx_selection_rejects_unsupported_runtime(
    agents: list[str], target: DeploymentTarget
) -> None:
    with pytest.raises(typer.BadParameter, match=r"support|host service"):
        habitat_step(
            agents,
            HabitatMode.sbx,
            "image@sha256:" + "a" * 64,
            interactive=False,
            target=target,
        )
