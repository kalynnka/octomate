"""One Codex process whose reader keeps running while a human answers."""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import NotRequired, Self, TypedDict

from openai_codex import AsyncCodex, CodexConfig
from openai_codex.client import CodexClient
from openai_codex.errors import CodexError
from pydantic import TypeAdapter

from octomate.tentacles.codex.telemetry import TracedCodexClient
from octomate.types.json import JsonObject

logger = logging.getLogger(__name__)


class ServerRequest(TypedDict):
    """The SDK's JSON-RPC envelope for a request from the app-server."""

    id: str | int
    method: str
    params: NotRequired[JsonObject | None]


server_request_adapter = TypeAdapter(ServerRequest)
type RequestHandler = Callable[
    [str, JsonObject | None], Coroutine[None, None, JsonObject]
]


class ConcurrentCodexClient(TracedCodexClient):
    """Dispatch server requests on the event loop, outside the SDK reader thread."""

    loop: asyncio.AbstractEventLoop
    handler: RequestHandler
    requests: set[asyncio.Task[None]]
    instrument: bool
    closing: bool

    def __init__(
        self, config: CodexConfig, handler: RequestHandler, *, instrument: bool
    ) -> None:
        super().__init__(config=config)
        self.handler = handler
        self.requests = set()
        self.instrument = instrument
        self.closing = False

    def _read_message(self) -> JsonObject:
        # The SDK invokes approval callbacks on its sole reader. Intercept only
        # server requests; its existing router still owns responses and events.
        while True:
            message = super()._read_message()
            if "method" not in message or "id" not in message:
                return message
            request = server_request_adapter.validate_python(message)
            self.loop.call_soon_threadsafe(self.dispatch, request)

    def dispatch(self, request: ServerRequest) -> None:
        if self.closing:
            return
        task = asyncio.create_task(self.respond(request))
        self.requests.add(task)
        task.add_done_callback(self.requests.discard)

    async def respond(self, request: ServerRequest) -> None:
        try:
            result = await self.handler(request["method"], request.get("params"))
            response: JsonObject = {"id": request["id"], "result": result}
        except Exception:
            logger.exception("Codex server request failed")
            response = {
                "id": request["id"],
                "error": {
                    "code": -32603,
                    "message": "Octomate could not answer the request.",
                },
            }
        try:
            await asyncio.to_thread(self._write_message, response)
        except (CodexError, OSError) as error:
            logger.warning("Codex server response could not be written", exc_info=True)
            self._router.fail_all(error)

    def _write_message(self, payload: JsonObject) -> None:
        if self.instrument:
            super()._write_message(payload)
        else:
            CodexClient._write_message(self, payload)


class SharedCodex(AsyncCodex):
    """One SDK client with asynchronous server requests and paired cleanup."""

    transport: ConcurrentCodexClient

    def __init__(
        self, config: CodexConfig, handler: RequestHandler, *, instrument: bool
    ) -> None:
        super().__init__(config=config)
        self.transport = ConcurrentCodexClient(config, handler, instrument=instrument)
        self._client._sync = self.transport

    async def __aenter__(self) -> Self:
        self.transport.loop = asyncio.get_running_loop()
        await super().__aenter__()
        return self

    async def close(self) -> None:
        self.transport.closing = True
        requests = list(self.transport.requests)
        for task in requests:
            task.cancel()
        await asyncio.gather(*requests, return_exceptions=True)
        await super().close()
