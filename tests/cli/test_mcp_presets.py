from __future__ import annotations

import subprocess
import sys

import pytest
import yaml
from octomate_cli.main import app
from pydantic import TypeAdapter
from typer.testing import CliRunner

from octomate import Octomate
from octomate.config import OAuthMcpConfig, OctomateConfig
from octomate.config.mcp.base import DeviceFlowConfig
from octomate.oauth.mcp import McpDeviceOAuthFlow
from octomate.tentacles.mcp import OAuthMcpTentacle, build_mcp


@pytest.mark.parametrize("read_only", [False, True])
def test_github_preset_generates_generic_oauth_config(read_only: bool) -> None:
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
    configs = TypeAdapter(dict[str, dict[str, OAuthMcpConfig]]).validate_python(
        yaml.safe_load(result.stdout)
    )
    config = configs["mcp"]["github_work"]
    assert config.type == "oauth"
    assert isinstance(config.flow, DeviceFlowConfig)
    assert config.client_id == "test-app"
    assert config.scopes == ["repo", "read:org"]
    assert config.scope_separator == ","
    assert str(config.url) == "https://api.githubcopilot.com/mcp/" + (
        "readonly" if read_only else ""
    )
    assert (
        str(config.flow.token_endpoint) == "https://github.com/login/oauth/access_token"
    )
    assert config.client_secret is None
    assert "bad_refresh_token" in config.invalid_credentials_errors
    host = Octomate()
    tentacle = build_mcp("github_work", config, host)
    assert type(tentacle) is OAuthMcpTentacle
    assert isinstance(host.oauth.connector("github_work").flow, McpDeviceOAuthFlow)
    OctomateConfig.model_validate(
        {
            "mcp": configs["mcp"],
            "oauth": {"encryption_key": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="},
        }
    )


def test_preset_prompts_for_client_id_and_quotes_yaml_values() -> None:
    result = CliRunner().invoke(
        app, ["mcp", "preset", "github", "--name", 'work: "one"'], input="test-app\n"
    )
    assert result.exit_code == 0, result.output
    assert '"work: \\"one\\""' in result.stdout
    assert 'client_id: "test-app"' in result.stdout


def test_preset_works_without_importing_the_server() -> None:
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
    assert "type: oauth" in result.stdout
