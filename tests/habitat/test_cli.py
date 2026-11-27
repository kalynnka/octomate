"""Host and credential commands remain independent when invoked directly."""

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import typer
import yaml
from octomate_cli import credentials, habitat
from octomate_cli import sbx as sbx_cli
from octomate_cli.habitat import HabitatProvider
from octomate_cli.installation import DeploymentTarget
from octomate_cli.main import app
from octomate_cli.wizard.agents import AGENTS
from octomate_cli.wizard.habitat import habitat_step
from typer.testing import CliRunner

from octomate.config.acp import AcpAgentConfig
from octomate.config.agents.claude import ClaudeCodeConfig
from octomate.config.agents.codex import CodexConfig
from octomate.config.agents.deepseek import DeepseekConfig
from octomate.config.agents.inkling import InklingConfig

runner = CliRunner()


@pytest.fixture
def sbx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Mock:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sbx_cli.sys, "platform", "darwin")
    monkeypatch.setattr(sbx_cli.os, "getuid", lambda: 501)
    monkeypatch.setattr(sbx_cli.shutil, "which", lambda name: f"/bin/{name}")

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
    monkeypatch.setattr(sbx_cli, "run_sbx", command_mock)
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
        app, ["habitat", "credentials", "sbx", "--service", "openai"], input="keep\ny\n"
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
        app, ["habitat", "credentials", "sbx", "--service", "openai", "--check"]
    )
    assert checked.exit_code == 0, checked.output
    repeated = runner.invoke(
        app, ["habitat", "credentials", "sbx", "--service", "openai"], input="keep\n"
    )
    assert repeated.exit_code == 0, repeated.output
    assert path.read_bytes() == before


def test_provider_login_and_approval_do_not_run_host_setup(
    sbx: Mock, tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        ["habitat", "credentials", "sbx", "--service", "openai"],
        input="oauth\ny\n",
    )
    assert result.exit_code == 0, result.output
    calls = [call.args for call in sbx.call_args_list]
    assert ("/bin/sbx", "login") not in calls
    assert all(command[1] in {"version", "secret"} for command in calls)
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
            "credentials",
            "sbx",
            "--service",
            "openai",
            *(["--check"] if check else []),
        ],
        input="" if check else "keep\nn\n",
    )
    assert result.exit_code != 0
    assert not (tmp_path / ".config/sbx/credentials.yaml").exists()


def test_malformed_binding_is_not_replaced_or_followed_by_login(
    sbx: Mock, tmp_path: Path
) -> None:
    path = tmp_path / ".config/sbx/credentials.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("bindings: []\n")
    result = runner.invoke(
        app, ["habitat", "credentials", "sbx", "--service", "openai"]
    )
    assert result.exit_code != 0
    assert "Cannot read sbx bindings" in result.output
    assert path.read_text() == "bindings: []\n"
    assert [call.args for call in sbx.call_args_list] == [("/bin/sbx", "version")]


def test_claude_reuses_its_existing_oauth_login(sbx: Mock, tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["habitat", "credentials", "sbx", "--service", "anthropic"], input="y\n"
    )
    assert result.exit_code == 0, result.output
    assert "existing sbx OAuth" in result.output
    assert not any("set" in call.args for call in sbx.call_args_list)


@pytest.mark.parametrize("check", [False, True])
def test_host_setup_needs_no_agent_or_credentials(
    sbx: Mock, tmp_path: Path, check: bool
) -> None:
    result = runner.invoke(
        app,
        ["habitat", "setup", "sbx", *(["--check"] if check else [])],
        input="" if check else "n\n",
    )
    assert result.exit_code == 0, result.output
    assert "Provider credentials are configured separately" in result.output
    calls = [call.args for call in sbx.call_args_list]
    assert ("/bin/sbx", "ls", "--quiet") in calls
    assert ("/bin/sbx", "settings", "get", "ssh.agentForwardingEnabled") in calls
    assert not any("secret" in call for call in calls)
    assert not (tmp_path / ".config/sbx/credentials.yaml").exists()


def test_host_login_ignores_provider_configuration(sbx: Mock, tmp_path: Path) -> None:
    path = tmp_path / ".config/sbx/credentials.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("bindings: []\n")
    result = runner.invoke(app, ["habitat", "setup", "sbx"], input="y\n")
    assert result.exit_code == 0, result.output
    assert ("/bin/sbx", "login") in [call.args for call in sbx.call_args_list]
    assert not any("secret" in call.args for call in sbx.call_args_list)
    assert path.read_text() == "bindings: []\n"


@pytest.mark.parametrize(
    "arguments",
    [
        ["setup", "sbx", "--agent", "codex"],
        ["credentials", "sbx", "--check"],
        ["credentials", "sbx", "--service", "acp"],
    ],
)
def test_commands_require_their_own_inputs(sbx: Mock, arguments: list[str]) -> None:
    result = runner.invoke(app, ["habitat", *arguments])
    assert result.exit_code == 2
    sbx.assert_not_called()


@pytest.mark.parametrize("agent", ["claude", "codex", "deepseek", "acp", "inkling"])
def test_sbx_selection_accepts_supported_agents(agent: str) -> None:
    image = "image@sha256:" + "a" * 64
    assert habitat_step(
        [agent],
        HabitatProvider.sbx,
        image,
        interactive=False,
        target=DeploymentTarget.launchd,
    ) == ({agent: HabitatProvider.sbx}, image)


@pytest.mark.parametrize("agent", ["claude", "codex", "deepseek"])
def test_srt_selection_accepts_supported_agents(agent: str) -> None:
    assert habitat_step(
        [agent],
        HabitatProvider.srt,
        None,
        interactive=False,
        target=DeploymentTarget.launchd,
    ) == ({agent: HabitatProvider.srt}, None)


@pytest.mark.parametrize("agent", ["acp", "inkling"])
def test_srt_selection_rejects_unsupported_agents(agent: str) -> None:
    with pytest.raises(typer.BadParameter, match="does not support workspace habitats"):
        habitat_step(
            [agent],
            HabitatProvider.srt,
            None,
            interactive=False,
            target=DeploymentTarget.launchd,
        )


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
    assert credentials.CREDENTIAL_APPROVAL_DOMAINS == expected


@pytest.mark.parametrize(
    ("agents", "target"),
    [(["unsupported"], DeploymentTarget.launchd), (["codex"], DeploymentTarget.docker)],
)
def test_sbx_selection_rejects_unsupported_runtime(
    agents: list[str], target: DeploymentTarget
) -> None:
    with pytest.raises(typer.BadParameter, match=r"support|host service"):
        habitat_step(
            agents,
            HabitatProvider.sbx,
            "image@sha256:" + "a" * 64,
            interactive=False,
            target=target,
        )


@pytest.mark.parametrize(
    ("assignments", "message"),
    [
        (["claude=unknown"], "native, srt or sbx"),
        (["other=native"], "selected AGENT=PROVIDER"),
        (["claude"], "selected AGENT=PROVIDER"),
        (["claude=srt", "claude=native"], "only once"),
    ],
)
def test_per_agent_habitat_rejects_invalid_assignments(
    assignments: list[str], message: str
) -> None:
    with pytest.raises(typer.BadParameter, match=message):
        habitat_step(
            ["claude", "codex", "deepseek"],
            None,
            None,
            assignments=assignments,
            interactive=False,
            target=DeploymentTarget.launchd,
        )


def test_native_override_does_not_inherit_sbx() -> None:
    image = "image@sha256:" + "a" * 64
    assert habitat_step(
        ["claude", "codex"],
        HabitatProvider.sbx,
        image,
        assignments=["claude=native"],
        interactive=False,
        target=DeploymentTarget.launchd,
    ) == ({"claude": HabitatProvider.native, "codex": HabitatProvider.sbx}, image)


@pytest.mark.parametrize("missing", ["srt", "node", "sdk", None])
def test_srt_setup_checks_runtime_installation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str | None,
) -> None:
    monkeypatch.setattr(habitat.platform, "system", lambda: "Darwin")
    probe = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(habitat.subprocess, "run", probe)
    entry = tmp_path / "cli.js"
    entry.touch()
    if missing != "sdk":
        entry.with_name("index.js").touch()
    monkeypatch.setattr(
        habitat.shutil, "which", lambda name: None if name == missing else str(entry)
    )
    result = runner.invoke(app, ["habitat", "setup", "srt", "--check"])
    assert result.exit_code == (0 if missing is None else 2), result.output
    if missing is None:
        assert "Runtime isolation is checked when a habitat starts" in result.output
        assert probe.call_args.args[0] == [
            str(entry),
            str(Path(habitat.__file__).with_name("srt.mjs")),
            entry.with_name("index.js").as_uri(),
        ]
    else:
        probe.assert_not_called()


@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_srt_setup_rejects_unqualified_platform_before_runtime_checks(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
) -> None:
    monkeypatch.setattr(habitat.platform, "system", lambda: system)
    lookup = Mock()
    monkeypatch.setattr(habitat.shutil, "which", lookup)
    result = runner.invoke(app, ["habitat", "setup", "srt", "--check"])
    assert result.exit_code == 2
    assert "macOS only" in result.output
    lookup.assert_not_called()


@pytest.mark.parametrize("returncode", [0, 1])
def test_srt_setup_surfaces_sdk_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    entry = tmp_path / "cli.js"
    entry.touch()
    entry.with_name("index.js").touch()
    monkeypatch.setattr(habitat.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(habitat.shutil, "which", lambda name: str(entry))
    monkeypatch.setattr(
        habitat.subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [], returncode, "", "runtime diagnostic"
            )
        ),
    )
    result = runner.invoke(app, ["habitat", "setup", "srt", "--check"])
    assert result.exit_code == (2 if returncode else 0)
    assert "runtime diagnostic" in result.output
    assert ("SRT dependency check failed" in result.output) == bool(returncode)


def test_standalone_wizard_contracts_match_agent_configurations() -> None:
    configurations = {
        "claude": ClaudeCodeConfig,
        "codex": CodexConfig,
        "deepseek": DeepseekConfig,
        "acp": AcpAgentConfig,
        "inkling": InklingConfig,
    }
    assert {name: setup.habitat_types for name, setup in AGENTS.items()} == {
        name: config.habitat_types for name, config in configurations.items()
    }
