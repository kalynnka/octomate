from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from octomate_cli.main import app
from typer.testing import CliRunner

from octomate import Octomate
from octomate.config import OAuthMcpConfig, OctomateConfig
from octomate.config.mcp.base import AuthorizationCodeFlowConfig, DeviceFlowConfig
from octomate.oauth.mcp import McpDeviceOAuthFlow, McpOAuthFlow
from octomate.tentacles.mcp import OAuthMcpTentacle, build_mcp


@pytest.fixture(autouse=True)
def preset_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCTOMATE_HOME", str(tmp_path))
    monkeypatch.setenv(
        "OCTOMATE__OAUTH__ENCRYPTION_KEY",
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
    )


@pytest.mark.parametrize("read_only", [False, True])
def test_github_preset_generates_generic_oauth_config(
    read_only: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "OCTOMATE__TENTACLES__GITHUB_WORK__CLIENT_SECRET", "test-app-secret"
    )
    monkeypatch.setenv("OCTOMATE__OAUTH__CALLBACK_BASE_URI", "http://localhost:8000")
    result = CliRunner().invoke(
        app,
        [
            "mcp",
            "preset",
            "github",
            "--client-id",
            "test-app",
            "--name",
            "github_work",
            *(["--read-only"] if read_only else []),
        ],
    )
    assert result.exit_code == 0, result.output
    configs = yaml.safe_load((tmp_path / "tentacles.yaml").read_text())
    assert "client_secret" not in configs["tentacles"]["github_work"]
    assert "test-app-secret" not in result.output
    assert "OCTOMATE__TENTACLES__GITHUB_WORK__CLIENT_SECRET" in result.output
    assert "/oauth/github_work/callback" in result.output
    config = OctomateConfig().tentacles["github_work"]
    assert isinstance(config, OAuthMcpConfig)
    assert config.type == "oauth"
    device, browser = config.flows
    assert isinstance(device, DeviceFlowConfig)
    assert isinstance(browser, AuthorizationCodeFlowConfig)
    assert (
        str(browser.authorization_endpoint)
        == "https://github.com/login/oauth/authorize"
    )
    assert browser.token_endpoint == device.token_endpoint
    assert browser.token_endpoint_auth_method == "client_secret_post"
    assert device.token_endpoint_auth_method == "none"
    assert config.client_id == "test-app"
    assert config.scopes == [
        "repo",
        "read:org",
        "read:user",
        "user:email",
        "write:packages",
        "project",
        "gist",
        "notifications",
        "workflow",
    ]
    assert config.scope_separator == ","
    assert str(config.url) == "https://api.githubcopilot.com/mcp/" + (
        "readonly" if read_only else ""
    )
    assert (
        str(config.flows[0].token_endpoint)
        == "https://github.com/login/oauth/access_token"
    )
    assert config.client_secret is not None
    assert config.client_secret.get_secret_value() == "test-app-secret"
    assert "bad_refresh_token" in config.invalid_credentials_errors
    host = Octomate()
    assert host.config.tentacles["github_work"] == config
    tentacle = build_mcp("github_work", config, host)
    assert type(tentacle) is OAuthMcpTentacle
    assert isinstance(
        host.oauth.connector("github_work").select_flow(), McpDeviceOAuthFlow
    )
    assert isinstance(
        host.oauth.connector("github_work").select_flow("authorization_code"),
        McpOAuthFlow,
    )
    OctomateConfig.model_validate(
        {
            "tentacles": configs["tentacles"],
            "oauth": {"encryption_key": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="},
        }
    )


def test_preset_prompts_for_client_id_and_quotes_yaml_values(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["mcp", "preset", "github", "--name", 'work: "one"'], input="test-app\n"
    )
    assert result.exit_code == 0, result.output
    saved = yaml.safe_load((tmp_path / "tentacles.yaml").read_text())
    assert saved["tentacles"]['work: "one"']["client_id"] == "test-app"


def test_preset_works_without_importing_the_server(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from typer.testing import CliRunner; from octomate_cli.main import app; "
            "result = CliRunner().invoke(app, ['mcp', 'preset', 'github', '--client-id', 'test-app']); "
            "assert result.exit_code == 0, result.output; assert 'octomate' not in sys.modules; print(result.output)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert str(tmp_path / "tentacles.yaml") in result.stdout
    assert "type: oauth" in (tmp_path / "tentacles.yaml").read_text()


def test_preset_preserves_other_tentacles_and_settings(tmp_path: Path) -> None:
    path = tmp_path / "tentacles.yaml"
    existing = {
        "tentacles": {
            "linear": {"type": "bare", "url": "https://mcp.linear.app/mcp"},
            "codex": {"type": "codex"},
        },
        "mcp_pool": {"idle_timeout": 3600},
    }
    path.write_text(yaml.safe_dump(existing))
    result = CliRunner().invoke(
        app, ["mcp", "preset", "github", "--client-id", "test-app"]
    )
    assert result.exit_code == 0, result.output
    saved = yaml.safe_load(path.read_text())
    assert saved["tentacles"]["linear"] == existing["tentacles"]["linear"]
    assert saved["tentacles"]["codex"] == existing["tentacles"]["codex"]
    assert saved["mcp_pool"] == existing["mcp_pool"]
    assert saved["tentacles"]["github"]["client_id"] == "test-app"


def test_preset_rejects_duplicate_without_modifying_file(tmp_path: Path) -> None:
    path = tmp_path / "tentacles.yaml"
    original = "tentacles:\n  github:\n    type: oauth\n    client_id: existing-app\n"
    path.write_text(original)
    result = CliRunner().invoke(
        app, ["mcp", "preset", "github", "--client-id", "test-app"]
    )
    assert result.exit_code == 2
    assert "already exists" in result.output
    assert path.read_text() == original


def test_preset_writes_to_explicit_destination(tmp_path: Path) -> None:
    path = tmp_path / "custom" / "tentacles.yaml"
    result = CliRunner().invoke(
        app,
        ["mcp", "preset", "github", "--client-id", "test-app", "--output", str(path)],
    )
    assert result.exit_code == 0, result.output
    assert (
        yaml.safe_load(path.read_text())["tentacles"]["github"]["client_id"]
        == "test-app"
    )
    assert not (tmp_path / "tentacles.yaml").exists()
