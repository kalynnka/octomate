from pathlib import Path

import httpx
import pytest

from octomate import Octomate
from octomate.config.channels import AgentModelConfig, TrunklineChannelConfig
from octomate.tentacles.trunkline import TrunklineTentacle


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "static_dir", [None, "{root}/console-assets", "console-assets", "~/console-assets"]
)
async def test_frontend_uses_the_enabled_channels_static_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    static_dir: str | None,
) -> None:
    directory = tmp_path / "console-assets"
    directory.mkdir()
    (directory / "index.html").write_text("<html>Trunkline</html>")
    (directory / "app.js").write_text("window.trunkline = true;")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    octomate = Octomate()
    if enabled:
        octomate.connect(
            TrunklineTentacle(
                "console",
                octomate,
                config=TrunklineChannelConfig.model_validate(
                    {
                        "static_dir": static_dir.format(root=tmp_path)
                        if static_dir is not None
                        else None,
                        "agents": [{"agent": "codex", "model": "gpt-5.6-sol"}],
                    }
                ),
            )
        )
    app = octomate.app()
    if enabled and static_dir is not None:
        assert app.url_path_for("console", path="app.js") == "/app.js"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        index = await client.get("/")
        asset = await client.get("/app.js")
        health = await client.get("/api/trunkline/health")
        mcp = await client.post("/octomate/mcp")
    assert index.status_code == (200 if enabled and static_dir is not None else 404)
    assert asset.status_code == index.status_code
    if enabled and static_dir is not None:
        assert index.text == "<html>Trunkline</html>"
        assert asset.text == "window.trunkline = true;"
    assert health.status_code == (200 if enabled else 404)
    assert mcp.status_code == 401


def test_frontend_rejects_a_missing_static_directory(tmp_path: Path) -> None:
    octomate = Octomate()
    octomate.connect(
        TrunklineTentacle(
            "trunkline",
            octomate,
            config=TrunklineChannelConfig(
                static_dir=tmp_path / "missing",
                agents=[AgentModelConfig(agent="codex", model="gpt-5.6-sol")],
            ),
        )
    )
    with pytest.raises(RuntimeError, match=r"Directory .* does not exist"):
        octomate.app()
