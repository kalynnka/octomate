from pathlib import Path

import pytest
from octomate_cli.tentacles.deepseek import deepseek_typer
from typer.testing import CliRunner


@pytest.mark.parametrize(
    "contents", ["{", "[]", "null", "{}", '{"name": 42}', '{"name": null}']
)
def test_invalid_bridge_manifest_is_a_cli_error(tmp_path: Path, contents: str) -> None:
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    (bridge / "package.json").write_text(contents)

    result = CliRunner().invoke(
        deepseek_typer,
        ["hooks", "install", "--home", str(tmp_path / "home"), "--bridge", "bridge"],
    )

    assert result.exit_code == 2
    assert "no readable package.json" in result.output
    assert not (tmp_path / "home" / "profiles" / "node_modules").exists()
