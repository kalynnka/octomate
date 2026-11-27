"""Guest process ownership and habitat-scoped DSH connection reuse."""

import asyncio
import json
import os
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import httpx2
import pytest
from pydantic import HttpUrl
from uuid_utils.compat import uuid7
from websockets.asyncio.client import ClientConnection

from octomate.config.agents.deepseek import DeepseekConfig
from octomate.config.habitat import SbxHabitatConfig, SrtHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.habitat.srt import SrtHabitat
from octomate.tentacles.deepseek import ink as ink_module
from octomate.tentacles.deepseek.client import DeepseekApiClient
from octomate.tentacles.deepseek.ink import DeepseekInk
from octomate.tentacles.deepseek.process import DeepseekProcess
from octomate.tentacles.deepseek.wire import MuxFrame
from tests.habitat.agents.test_claude import executable as executable
from tests.habitat.test_contracts import IMAGE


def habitat(tmp_path: Path, executable: str = "sbx") -> SbxHabitat:
    return SbxHabitat(
        config=SbxHabitatConfig(image=IMAGE, executable=executable),
        user_id=uuid7(),
        root=tmp_path,
        config_dir=tmp_path,
    )


async def test_guest_owns_private_home_listener_and_child(
    tmp_path: Path, executable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "dsh"
    binary.write_text(
        f"#!{sys.executable}\n"
        + Path(__file__)
        .parents[2]
        .joinpath("agent/deepseek/fixtures/web_process.py")
        .read_text()
    )
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}:{os.environ['PATH']}")
    published: list[int] = []

    @asynccontextmanager
    async def publish(self: SbxHabitat, port: int) -> AsyncGenerator[str]:
        published.append(port)
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            published.remove(port)

    monkeypatch.setattr(SbxHabitat, "publish", publish)
    process = DeepseekProcess(
        executable=str(binary),
        port=3081,
        extra_args=[],
        dsh_home=tmp_path / ".dsh",
        ready_timeout=5,
        habitat=habitat(tmp_path, str(executable)),
        env={"DSH_TEST_LOG": str(tmp_path / "process.json")},
    )
    endpoint = await process.start()
    try:
        assert endpoint.port in published
        assert process.launch_token is not None
        assert process.launch_token.get_secret_value() == "guest-token"
        async with httpx2.AsyncClient() as client:
            response = await client.get(str(endpoint))
            assert response.text == "guest-ready"
        info = json.loads((tmp_path / "process.json").read_text())
        home = Path(info["home"])
        assert home != process.dsh_home
        assert await anyio.Path(home).is_dir()
        assert info["cwd"] == str(tmp_path)
        settings = next(item for item in info["patch"] if item["id"] == "settings")
        assert settings["config"]["path"] == str(process.dsh_home / "settings.yaml")
    finally:
        await asyncio.wait_for(process.stop(), 10)
    assert not published
    assert not await anyio.Path(home).exists()
    with pytest.raises(ProcessLookupError):
        os.kill(info["pid"], 0)


@pytest.mark.parametrize("srt", [False, True])
async def test_pool_reuses_each_habitat_routes_sessions_and_releases_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, srt: bool
) -> None:
    processes: list[DeepseekProcess] = []
    stopped: list[DeepseekProcess] = []
    clients: list[AsyncMock] = []

    async def start(process: DeepseekProcess) -> HttpUrl:
        processes.append(process)
        return HttpUrl(f"http://127.0.0.1:{4000 + len(processes)}")

    async def stop(process: DeepseekProcess) -> None:
        stopped.append(process)

    async def frames(socket: ClientConnection) -> AsyncIterator[tuple[str, MuxFrame]]:
        queue: asyncio.Queue[tuple[str, MuxFrame]] = asyncio.Queue()
        while True:
            yield await queue.get()

    def client(base_url: HttpUrl, http_client: httpx2.AsyncClient) -> AsyncMock:
        mock = AsyncMock(spec=DeepseekApiClient)
        mock.base_url = base_url
        mock.mux_frames.side_effect = frames

        async def close(*args: None) -> None:
            await http_client.aclose()

        mock.__aexit__.side_effect = close
        clients.append(mock)
        return mock

    monkeypatch.setattr(DeepseekProcess, "start", start)
    monkeypatch.setattr(DeepseekProcess, "stop", stop)
    monkeypatch.setattr(ink_module, "DeepseekApiClient", client)
    first, second = (
        (
            SrtHabitat(config=SrtHabitatConfig(), user_id=uuid7(), root=tmp_path),
            SrtHabitat(config=SrtHabitatConfig(), user_id=uuid7(), root=tmp_path),
        )
        if srt
        else (habitat(tmp_path), habitat(tmp_path))
    )
    env = {"DSH_HOME": str(tmp_path / ".dsh")}
    async with DeepseekInk(
        DeepseekConfig(port=0),
        answer_interaction=AsyncMock(),
        invalidate_commands=lambda: None,
        native=False,
    ) as ink:
        connections = await asyncio.gather(
            *(
                ink.acquire(first, env=env, session_id=f"session-{index}")
                for index in range(4)
            )
        )
        assert all(connection is connections[0] for connection in connections)
        other = await ink.acquire(second, env=env, session_id="other")
        assert other is not connections[0]
        assert len(processes) == 3  # core discovery plus the two habitats
        if srt:
            assert all(process.port == 0 for process in processes[1:])
        assert await ink.acquire(session_id="session-1") is connections[0]
        assert await ink.acquire(session_id="other") is other
        assert connections[0].client.base_url == HttpUrl("http://127.0.0.1:4002")
        with pytest.raises(ValueError, match="another habitat"):
            await ink.acquire(second, env=env, session_id="session-1")
        await first.resources.aclose()
        assert not connections[0].running
        assert other.running
        replacement = await ink.acquire(first, env=env, session_id="session-1")
        assert replacement is not connections[0]
        assert processes[-1].dsh_home == tmp_path / ".dsh"
    await first.resources.aclose()
    await second.resources.aclose()
    assert len(stopped) == 4
    for mock in clients:
        mock.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("silent", [False, True])
async def test_failed_guest_start_reaps_child_and_preserves_diagnostics(
    tmp_path: Path,
    executable: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    silent: bool,
) -> None:
    binary = tmp_path / "dsh"
    info_path = tmp_path / "failed.json"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, time, sys\n"
        "from pathlib import Path\n"
        f"Path({str(info_path)!r}).write_text(json.dumps({{'pid': os.getpid(), 'home': os.environ['DSH_HOME']}}))\n"
        "print('provider initialization failed', file=sys.stderr, flush=True)\n"
        + ("time.sleep(60)\n" if silent else "sys.exit(17)\n")
    )
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}:{os.environ['PATH']}")
    process = DeepseekProcess(
        executable=str(binary),
        port=0,
        extra_args=[],
        dsh_home=tmp_path / ".dsh",
        ready_timeout=5,
        habitat=habitat(tmp_path, str(executable)),
    )
    with pytest.raises(TimeoutError if silent else RuntimeError):
        await process.start()
    info = json.loads(info_path.read_text())
    assert "provider initialization failed" in caplog.text
    assert not await anyio.Path(info["home"]).exists()
    with pytest.raises(ProcessLookupError):
        os.kill(info["pid"], 0)
