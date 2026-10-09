from pathlib import Path

import httpx
import pytest

from octomate import Octomate
from octomate.config.channels import TrunklineChannelConfig
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
                        "agents": ["codex"],
                    }
                ),
            )
        )
    app = octomate
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
        assert app.url_path_for("console", path="app.js") == "/app.js"
        assert index.text == "<html>Trunkline</html>"
        assert asset.text == "window.trunkline = true;"
    assert health.status_code == (503 if enabled else 404)
    assert mcp.status_code == 401


def test_frontend_rejects_a_missing_static_directory(tmp_path: Path) -> None:
    octomate = Octomate()
    octomate.connect(
        TrunklineTentacle(
            "trunkline",
            octomate,
            config=TrunklineChannelConfig(
                static_dir=tmp_path / "missing",
                agents=["codex"],
            ),
        )
    )
    with pytest.raises(RuntimeError, match=r"Directory .* does not exist"):
        octomate.build_middleware_stack()


async def test_a_console_page_reloads_into_the_console(tmp_path: Path) -> None:
    """A thread or control page the console's address names is no file, so a
    browser reloading it is answered with the console. A miss nobody navigated to
    stays a 404 — an unknown API path or asset is never answered with a page."""
    (tmp_path / "index.html").write_text("<html>Trunkline</html>")
    (tmp_path / "app.js").write_text("window.trunkline = true;")
    octomate = Octomate()
    octomate.connect(
        TrunklineTentacle(
            "trunkline",
            octomate,
            config=TrunklineChannelConfig(static_dir=tmp_path, agents=["codex"]),
        )
    )
    page = {"Accept": "text/html,application/xhtml+xml"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=octomate), base_url="http://test"
    ) as client:
        thread = await client.get(
            "/threads/01a117fb-8ed6-7b82-be5f-6db7923b94d9", headers=page
        )
        control = await client.get("/control/agents", headers=page)
        asset = await client.get("/app.js", headers=page)
        missing_asset = await client.get("/missing.js")
        missing_api = await client.get(
            "/api/trunkline/missing", headers={"Accept": "application/json"}
        )
    assert (thread.status_code, thread.text) == (200, "<html>Trunkline</html>")
    assert (control.status_code, control.text) == (200, "<html>Trunkline</html>")
    assert asset.text == "window.trunkline = true;"
    assert (missing_asset.status_code, missing_api.status_code) == (404, 404)
