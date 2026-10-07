"""One Codex process whose reader keeps running while a human answers."""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from typing import NamedTuple, NotRequired, Self, TypedDict

from openai_codex import AsyncCodex, CodexConfig
from openai_codex._message_router import _TurnSubscription
from openai_codex.client import CodexClient
from openai_codex.errors import CodexError, InvalidRequestError, TransportClosedError
from openai_codex.generated.v2_all import (
    ReviewDelivery,
    ReviewStartParams,
    ReviewStartResponse,
    ReviewTarget,
    TurnStartedNotification,
)
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


class CommandTurn(NamedTuple):
    """Native turn identity and the SDK cursor retaining its earliest events."""

    id: str
    subscription: _TurnSubscription


class ConcurrentCodexClient(TracedCodexClient):
    """Dispatch server requests on the event loop, outside the SDK reader thread."""

    loop: asyncio.AbstractEventLoop
    handler: RequestHandler
    requests: set[asyncio.Task[None]]
    instrument: bool
    closing: bool
    compact_starts: dict[str, Future[str]]  # Compaction ACKs omit the turn ID.

    def __init__(
        self, config: CodexConfig, handler: RequestHandler, *, instrument: bool
    ) -> None:
        super().__init__(config=config)
        self.handler = handler
        self.requests = set()
        self.instrument = instrument
        self.closing = False
        self.compact_starts = {}

    def _reader_loop(self) -> None:
        try:
            super()._reader_loop()
        finally:
            for started in list(self.compact_starts.values()):
                if not started.done():
                    started.set_exception(
                        TransportClosedError("Codex closed before compaction started")
                    )

    def _read_message(self) -> JsonObject:
        # The SDK invokes approval callbacks on its sole reader. Dispatch them
        # outside that reader; its existing router still owns responses and events.
        while True:
            message = super()._read_message()
            if message.get("method") == "turn/started" and "id" not in message:
                payload = TurnStartedNotification.model_validate(message.get("params"))
                if started := self.compact_starts.pop(payload.thread_id, None):
                    started.set_result(payload.turn.id)
            if "method" not in message or "id" not in message:
                return message
            request = server_request_adapter.validate_python(message)
            self.loop.call_soon_threadsafe(self.dispatch, request)

    def start_command(
        self, thread_id: str, review: ReviewTarget | None = None
    ) -> CommandTurn:
        """Subscribe before review/compact dispatch, using the SDK's turn router.

        These RPCs have no SDK turn-handle API. Compaction returns an empty ACK;
        its turn ID comes from turn/started. Neither ACK means completion.
        """
        with (
            self._thread_start_lock(thread_id),
            self._router.pending_turn(thread_id) as cursors,
        ):
            if self._router.has_goal(thread_id):
                raise InvalidRequestError(-32600, "thread has an active goal operation")
            if review is None:
                started: Future[str] = Future()
                self.compact_starts[thread_id] = started
                try:
                    self.thread_compact(thread_id)
                    turn_id = started.result()
                finally:
                    self.compact_starts.pop(thread_id, None)
            else:
                response = self.request(
                    "review/start",
                    ReviewStartParams(
                        thread_id=thread_id,
                        target=review,
                        delivery=ReviewDelivery.inline,
                    ).model_dump(mode="json", by_alias=True),
                    response_model=ReviewStartResponse,
                )
                if response.review_thread_id != thread_id:
                    raise ValueError("Codex review returned another thread")
                turn_id = response.turn.id
            subscription = self._router.prepare_turn(
                turn_id, thread_id, cursors, for_handle=True
            )
            assert subscription is not None
            return CommandTurn(turn_id, subscription)

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
