"""One Codex process whose reader keeps running while a human answers."""

import asyncio
import logging
import sys
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from contextlib import AsyncExitStack
from types import TracebackType
from typing import NamedTuple, NotRequired, Self, TypedDict

import anyio
from anyio import from_thread
from anyio.abc import ByteStream
from anyio.lowlevel import EventLoopToken, current_token
from anyio.streams.buffered import BufferedByteReceiveStream
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
from pydantic import TypeAdapter, ValidationError

from octomate.habitat import ManagedHabitat
from octomate.tentacles.codex.adapter import json_object_adapter
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

    token: EventLoopToken | None  # SDK threads enter the owning async runtime.
    handler: RequestHandler
    requests: set[asyncio.Task[None]]
    instrument: bool
    closing: bool
    compact_starts: dict[str, Future[str]]  # Compaction ACKs omit the turn ID.
    stream: ByteStream | None  # Guest stdio; None retains the SDK's local subprocess.
    reader: BufferedByteReceiveStream | None
    reader_thread: (
        threading.Thread | None
    )  # Guest reader; the SDK owns its local reader.

    def __init__(
        self, config: CodexConfig, handler: RequestHandler, *, instrument: bool
    ) -> None:
        super().__init__(config=config)
        self.token = None
        self.handler = handler
        self.requests = set()
        self.instrument = instrument
        self.closing = False
        self.compact_starts = {}
        self.stream = None
        self.reader = None
        self.reader_thread = None

    @property
    def thread_token(self) -> EventLoopToken:
        if self.token is None:
            raise TransportClosedError("Codex async runtime is not connected")
        return self.token

    def start(self) -> None:
        if self.stream is None:
            super().start()
            return
        if self.reader_thread is not None:
            return
        self.reader = BufferedByteReceiveStream(self.stream)
        self.reader_thread = threading.Thread(target=self.reader_loop, daemon=True)
        self.reader_thread.start()

    def close(self) -> None:
        if self.stream is None:
            super().close()
            return
        self._runtime_version = None
        self._router.fail_all(TransportClosedError("Codex habitat client closed"))
        from_thread.run(self.stream.aclose, token=self.thread_token)
        if self.reader_thread is not None:
            self.reader_thread.join()

    async def read_frame(self) -> JsonObject:
        """Read one native JSON-RPC line, including full transcript responses."""
        if self.reader is None:
            raise TransportClosedError("Codex habitat reader is not running")
        try:
            frame = await self.reader.receive_until(b"\n", sys.maxsize)
        except anyio.IncompleteRead:
            if self.reader.buffer:
                raise CodexError("Incomplete Codex protocol frame") from None
            raise TransportClosedError(
                f"Codex closed stdout. stderr_tail={self._stderr_tail()[:2000]}"
            ) from None
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            raise TransportClosedError("Codex habitat stream closed") from None
        try:
            return json_object_adapter.validate_json(frame)
        except ValidationError:
            raise CodexError("Invalid Codex protocol frame") from None

    def reader_loop(self) -> None:
        try:
            super()._reader_loop()
        finally:
            for started in list(self.compact_starts.values()):
                if not started.done():
                    started.set_exception(
                        TransportClosedError("Codex closed before compaction started")
                    )

    # The SDK starts its local reader through this hook.
    _reader_loop = reader_loop

    def _read_message(self) -> JsonObject:
        # The SDK invokes approval callbacks on its sole reader. Dispatch them
        # outside that reader; its existing router still owns responses and events.
        while True:
            message = (
                super()._read_message()
                if self.stream is None
                else from_thread.run(self.read_frame, token=self.thread_token)
            )
            if message.get("method") == "turn/started" and "id" not in message:
                payload = TurnStartedNotification.model_validate(message.get("params"))
                if started := self.compact_starts.pop(payload.thread_id, None):
                    started.set_result(payload.turn.id)
            if "method" not in message or "id" not in message:
                return message
            request = server_request_adapter.validate_python(message)
            from_thread.run_sync(self.dispatch, request, token=self.thread_token)

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
        if self.stream is not None:
            if self.instrument:
                payload = self.trace_message(payload)
            frame = json_object_adapter.dump_json(payload)
            with self._lock:
                try:
                    from_thread.run(
                        self.stream.send, frame + b"\n", token=self.thread_token
                    )
                except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                    raise TransportClosedError("Codex habitat stream closed") from None
            return
        if self.instrument:
            super()._write_message(payload)
        else:
            CodexClient._write_message(self, payload)


class SharedCodex(AsyncCodex):
    """One reusable SDK client and app-server connection, local or in a habitat.

    Construct once per runtime configuration and share across requests. A habitat
    client keeps its stdio attachment until explicit close or habitat shutdown.
    Closed clients cannot restart; a new runtime lifetime needs a new client.
    """

    transport: ConcurrentCodexClient
    habitat: ManagedHabitat | None
    connection: AsyncExitStack
    lifecycle_lock: asyncio.Lock

    def __init__(
        self,
        config: CodexConfig,
        handler: RequestHandler,
        *,
        instrument: bool,
        habitat: ManagedHabitat | None = None,
    ) -> None:
        super().__init__(config=config)
        self.transport = ConcurrentCodexClient(config, handler, instrument=instrument)
        self._client._sync = self.transport
        self.habitat = habitat
        self.connection = AsyncExitStack()
        self.lifecycle_lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def start(self) -> None:
        """Initialize once, including when concurrent callers first use the SDK."""
        async with self.lifecycle_lock:
            if self.transport.closing:
                raise TransportClosedError("Codex client is closed")
            if self._initialized:
                return
            self.transport.token = current_token()
            try:
                await self.connect()
                await super()._ensure_initialized()
            except BaseException as error:
                await self.shutdown(type(error), error, error.__traceback__)
                raise
            if self.habitat is not None:
                self.habitat.resources.push_async_callback(self.close)

    async def connect(self) -> None:
        """Attach guest stdio before the SDK starts its reader and initializes."""
        if self.habitat is None:
            return
        config = self.transport.config
        if config.cwd is None:
            raise ValueError("Habitat Codex clients require a guest working directory")
        if config.launch_args_override is not None:
            command = list(config.launch_args_override)
        else:
            command = [config.codex_bin or "codex"]
            for value in config.config_overrides:
                command.extend(["--config", value])
            command.extend(["app-server", "--listen", "stdio://"])
        self.transport.stream = await self.connection.enter_async_context(
            self.habitat.execute(
                *command,
                workdir=config.cwd,
                env={**(config.env or {}), "PWD": config.cwd},
                stderr=self.transport._stderr_lines.append,
            )
        )

    # SDK APIs initialize lazily through this hook as well as explicit start().
    _ensure_initialized = start

    async def shutdown(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Drain SDK work, then release the thread token and stdio attachment."""
        self.transport.closing = True
        with anyio.CancelScope(shield=True):
            requests = list(self.transport.requests)
            for task in requests:
                task.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
            try:
                await super().close()
            finally:
                self.transport.token = None
                await self.connection.__aexit__(exc_type, exc_value, traceback)

    async def close(self) -> None:
        await self.__aexit__(None, None, None)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        with anyio.CancelScope(shield=True):
            async with self.lifecycle_lock:
                if not self.transport.closing:
                    await self.shutdown(exc_type, exc_value, traceback)
