"""Keep the HTTP listener available for tentacle startup and shutdown hooks."""

from __future__ import annotations

import socket
from contextlib import AsyncExitStack

import uvicorn
from uvicorn.supervisors import ChangeReload, Multiprocess

from octomate.base import Octomate


class Server(uvicorn.Server):
    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.tentacles: AsyncExitStack = AsyncExitStack()

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.should_exit:
            return
        app = self.lifespan.state["octomate"]
        if not isinstance(app, Octomate):
            raise TypeError("The server requires an Octomate application")
        await self.tentacles.enter_async_context(app.run_tentacles())

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        try:
            await self.tentacles.aclose()
        finally:
            await super().shutdown(sockets=sockets)


def run(config: uvicorn.Config) -> None:
    """Serve Octomate, including the same lifecycle in reload subprocesses."""
    server = Server(config)
    if config.should_reload or config.workers > 1:
        supervisor = ChangeReload if config.should_reload else Multiprocess
        with config.bind_socket() as sock:
            supervisor(config, target=server.run, sockets=[sock]).run()
    else:
        server.run()
        if not server.started:
            raise SystemExit(3)
