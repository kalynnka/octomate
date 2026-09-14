from __future__ import annotations

import asyncio
import socket
from types import TracebackType
from typing import Self
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.base import Octomate
from octomate.config.base import OctomateConfig
from octomate.server import Server, run
from octomate.tentacles.base import Tentacle
from tests.support.agents import FakeAgent
from tests.support.channels import FakeChannelTentacle
from tests.support.users import a_api_key, a_user, auth_config


async def test_hooks_and_mcp_serve_during_tentacle_entry_and_exit(
    in_memory_engine: AsyncEngine,
) -> None:
    entered: list[str] = []
    exited: list[str] = []
    ready = asyncio.Event()
    app = Octomate(config=OctomateConfig(auth=auth_config()))
    user = await a_user("lu")
    await a_api_key(user, "test-token")

    @app.post("/test-hook")
    async def hook() -> dict[str, bool]:
        return {"ok": True}

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"

        class CallbackAgent(FakeAgent):
            async def __aenter__(self) -> Self:
                async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
                    response = await client.post("/test-hook")
                    assert response.json() == {"ok": True}
                    response = await client.post(
                        "/octomate/mcp",
                        headers={
                            "Authorization": "Bearer test-token",
                            "Accept": "application/json, text/event-stream",
                        },
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    )
                    assert response.status_code == 200
                    assert "tools" in response.text
                self.models = {"discovered": "fake-model"}
                entered.append("agent")
                return await super().__aenter__()

            async def __aexit__(
                self,
                exc_type: type[BaseException] | None = None,
                exc_value: BaseException | None = None,
                traceback: TracebackType | None = None,
            ) -> None:
                async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
                    response = await client.post("/test-hook")
                    assert response.json() == {"ok": True}
                exited.append("agent")
                await super().__aexit__(exc_type, exc_value, traceback)

        class CallbackIntegration(Tentacle[None, None]):
            async def __aenter__(self) -> Self:
                async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
                    response = await client.post("/test-hook")
                    assert response.json() == {"ok": True}
                entered.append("integration")
                return self

        class ReadyChannel(FakeChannelTentacle):
            async def probe(self) -> None:
                assert list(app.agents["inkling"].models) == ["discovered"]
                async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
                    response = await client.post("/test-hook")
                    assert response.json() == {"ok": True}
                await super().probe()
                entered.append("channel")
                ready.set()

            async def __aexit__(
                self,
                exc_type: type[BaseException] | None = None,
                exc_value: BaseException | None = None,
                traceback: TracebackType | None = None,
            ) -> None:
                exited.append("channel")
                await super().__aexit__(exc_type, exc_value, traceback)

        app.connect(ReadyChannel(octomate=app))
        app.connect(CallbackAgent(octomate=app, models={}))
        app.connect(CallbackIntegration(id="integration", octomate=app))
        server = Server(uvicorn.Config(app, lifespan="on", log_config=None))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            await asyncio.wait_for(ready.wait(), timeout=10)
            assert set(entered[:2]) == {"agent", "integration"}
            assert entered[2:] == ["channel"]
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=10)

    assert exited == ["channel", "agent"]
    assert not server.lifespan.error_occurred


@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_failed_tentacle_does_not_prevent_other_starts(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    class FailingAgent(FakeAgent):
        async def __aenter__(self) -> Self:
            if failure == "timeout":
                await asyncio.Event().wait()
            raise RuntimeError("startup failed")

    app = Octomate()
    agent = FakeAgent(octomate=app)
    app.connect(FailingAgent(id="failing", octomate=app))
    app.connect(agent)
    monkeypatch.setattr("octomate.base.TENTACLE_START_TIMEOUT", 0.05)

    async with app.run_tentacles():
        assert agent.routes
    assert agent.routes == []


async def test_api_startup_failure_does_not_enter_tentacles() -> None:
    app = Octomate()
    server = Server(uvicorn.Config(app, lifespan="on", log_config=None))
    with (
        patch.object(
            app.projects, "reconcile", new=AsyncMock(side_effect=RuntimeError("fail"))
        ),
        patch.object(app, "run_tentacles") as tentacles,
    ):
        await server.serve()

    tentacles.assert_not_called()
    assert server.should_exit
    assert not server.started


async def test_bind_failure_does_not_enter_tentacles(
    in_memory_engine: AsyncEngine,
) -> None:
    app = Octomate()
    with socket.socket() as sock, patch.object(app, "run_tentacles") as tentacles:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        server = Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=sock.getsockname()[1],
                lifespan="on",
                log_config=None,
            )
        )
        with pytest.raises(SystemExit, match="1"):
            await server.serve()

    tentacles.assert_not_called()
    assert not server.started
    assert server.lifespan.shutdown_event.is_set()
    assert not server.lifespan.error_occurred


@pytest.mark.parametrize("reload", [True, False])
def test_subprocesses_use_the_octomate_server(reload: bool) -> None:
    config = uvicorn.Config(
        "octomate.app:create_app",
        factory=True,
        reload=reload,
        workers=1 if reload else 2,
    )
    supervisor = "ChangeReload" if reload else "Multiprocess"
    with (
        patch("octomate.server.Server") as server_type,
        patch(f"octomate.server.{supervisor}") as supervisor_type,
        patch.object(config, "bind_socket") as bind_socket,
    ):
        run(config)

    supervisor_type.assert_called_once_with(
        config,
        target=server_type.return_value.run,
        sockets=[bind_socket.return_value.__enter__.return_value],
    )
    supervisor_type.return_value.run.assert_called_once_with()
    server_type.return_value.run.assert_not_called()
