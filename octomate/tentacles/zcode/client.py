from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import TracebackType

import anyio
from pydantic import SecretStr, ValidationError
from pydantic_ai.exceptions import AgentRunError

from octomate.tentacles.zcode.wire import (
    InteractionRequest,
    PermissionRequest,
    RpcNotification,
    RpcRequest,
    RpcResponse,
    SessionEvent,
    interaction_request_adapter,
    json_object_adapter,
)
from octomate.types.json import JsonObject, JsonValue


class ZcodeClient:
    """One owned app-server, with a reader independent of outstanding RPC calls."""

    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path,
        state_dir: Path,
        request_timeout: float,
        secrets: list[SecretStr],
        interaction_handler: Callable[[InteractionRequest], Awaitable[JsonObject]]
        | None = None,
    ) -> None:
        self.command: list[str] = command
        self.cwd: Path = cwd
        self.state_dir: Path = state_dir
        self.request_timeout: float = request_timeout
        self.secrets: list[SecretStr] = secrets
        self.interaction_handler: (
            Callable[[InteractionRequest], Awaitable[JsonObject]] | None
        ) = interaction_handler
        self.interactions: dict[
            tuple[str, str, str], tuple[InteractionRequest, asyncio.Task[JsonObject]]
        ] = {}
        self.callback_tasks: set[asyncio.Task[None]] = set()
        self.close_lock: asyncio.Lock = asyncio.Lock()
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None
        self.stderr_reader: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[JsonValue]] = {}
        self.events: asyncio.Queue[SessionEvent | AgentRunError] = asyncio.Queue()
        self.write_lock: asyncio.Lock = asyncio.Lock()
        self.next_id: int = 0
        self.failure: AgentRunError | None = None
        self.stderr_tail: str = ""
        self.closing: bool = False

    async def __aenter__(self) -> ZcodeClient:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "ZCODE_STORAGE_DIR": str(self.state_dir),
            "ZCODE_SESSION_DB_PATH": str(self.state_dir / "sessions.db"),
        }
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=self.cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=16 * 1024 * 1024,
                start_new_session=True,
            )
        except OSError as error:
            raise AgentRunError(
                f"Cannot launch ZCode: {self.redact(str(error))}"
            ) from None
        self.reader = asyncio.create_task(self.read_frames())
        self.stderr_reader = asyncio.create_task(self.read_stderr())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        with anyio.CancelScope(shield=True):
            async with self.close_lock:
                await self.close()

    async def close(self) -> None:
        if not self.closing:
            self.closing = True
            if self.reader is not None:
                self.reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.reader
            tasks = [task for _, task in self.interactions.values()]
            for task in [*self.callback_tasks, *tasks]:
                task.cancel()
            await asyncio.gather(*self.callback_tasks, *tasks, return_exceptions=True)
            self.callback_tasks.clear()
            self.interactions.clear()
            process = self.process
            if process is not None and process.returncode is None:
                # The runtime may own tool subprocesses. Its process group belongs to this run.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            if self.stderr_reader is not None:
                self.stderr_reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.stderr_reader
            for future in self.pending.values():
                if not future.done():
                    future.cancel()
            self.pending.clear()

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            value = secret.get_secret_value()
            if value:
                text = text.replace(value, "[redacted]")
        return text

    async def send(self, frame: JsonObject) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise AgentRunError("ZCode client is not running")
        async with self.write_lock:
            process.stdin.write((json.dumps(frame) + "\n").encode())
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                raise AgentRunError("ZCode closed its input stream") from None

    async def call(self, method: str, params: JsonObject) -> JsonValue:
        if self.failure is not None:
            raise self.failure
        self.next_id += 1
        request_id = self.next_id
        future: asyncio.Future[JsonValue] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            try:
                return await asyncio.wait_for(future, timeout=self.request_timeout)
            except TimeoutError:
                raise AgentRunError(f"ZCode {method} timed out") from None
        finally:
            self.pending.pop(request_id, None)

    async def read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        while chunk := await process.stderr.read(4096):
            self.stderr_tail = self.redact(
                (self.stderr_tail + chunk.decode(errors="replace"))[-4096:]
            )

    async def read_frames(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            while line := await process.stdout.readline():
                raw = json_object_adapter.validate_json(line)
                if "method" in raw and "id" in raw:
                    request = RpcRequest.model_validate(raw)
                    if request.method in {
                        "interaction/requestPermission",
                        "interaction/requestUserInput",
                    }:
                        try:
                            interaction = interaction_request_adapter.validate_json(
                                self.redact(json.dumps(raw))
                            )
                        except ValidationError:
                            await self.send(
                                {
                                    "id": request.id,
                                    "error": {
                                        "code": -32602,
                                        "message": "Invalid ZCode interaction payload",
                                    },
                                }
                            )
                            raise AgentRunError(
                                "Invalid ZCode interaction payload"
                            ) from None
                        key = (
                            interaction.method,
                            interaction.params.session_id,
                            interaction.params.request_id,
                        )
                        existing = self.interactions.get(key)
                        if existing is None:
                            task = asyncio.create_task(
                                self.answer_interaction(interaction)
                            )
                            self.interactions[key] = (interaction, task)
                        else:
                            previous, task = existing
                            if previous != interaction:
                                raise AgentRunError(
                                    "ZCode changed an outstanding interaction"
                                )
                        reply = asyncio.create_task(
                            self.reply_interaction(request.id, task)
                        )
                        self.callback_tasks.add(reply)
                        reply.add_done_callback(self.callback_tasks.discard)
                        continue
                    if request.method == "session/requestRuntimePreferences":
                        result: JsonObject = {
                            "nativeSearchEnhancementsEnabled": False,
                            "memoryEnabled": False,
                            "askUserQuestionAutoResolutionEnabled": False,
                            "modelContextBudgetStrategy": "preflight-v1",
                        }
                    else:
                        await self.send(
                            {
                                "id": request.id,
                                "error": {
                                    "code": -32601,
                                    "message": f"Unsupported ZCode callback: {request.method}",
                                },
                            }
                        )
                        if request.method in {
                            "interaction/requestProviderRuntimeHeaders",
                            "interaction/requestOfficialMcpAuthHeaders",
                        }:
                            raise AgentRunError(
                                "ZCode desktop-login authentication callbacks are not supported"
                            )
                        continue
                    await self.send({"id": request.id, "result": result})
                elif "id" in raw:
                    if ("result" in raw) == ("error" in raw):
                        raise AgentRunError("ZCode sent an invalid RPC response")
                    response = RpcResponse.model_validate(raw)
                    future = (
                        self.pending.get(response.id)
                        if isinstance(response.id, int)
                        else None
                    )
                    if future is None or future.done():
                        continue
                    if response.error is not None:
                        future.set_exception(
                            AgentRunError(
                                f"ZCode RPC failed ({response.error.code}): {self.redact(response.error.message)}"
                            )
                        )
                    else:
                        future.set_result(response.result)
                elif "method" in raw:
                    notification = RpcNotification.model_validate(raw)
                    if notification.method == "session/event":
                        await self.events.put(
                            SessionEvent.model_validate(notification.params)
                        )
                else:
                    raise AgentRunError("ZCode sent an invalid RPC envelope")
            if not self.closing:
                raise AgentRunError(
                    f"ZCode event stream closed unexpectedly. {self.stderr_tail}".strip()
                )
        except (ValidationError, ValueError):
            self.fail(AgentRunError("ZCode sent an invalid protocol frame"))
        except (AgentRunError, OSError) as error:
            self.fail(AgentRunError(self.redact(str(error))))

    def fail(self, error: AgentRunError) -> None:
        if self.failure is not None:
            return
        self.failure = error
        for future in self.pending.values():
            if not future.done():
                future.set_exception(error)
        self.events.put_nowait(error)

    async def answer_interaction(self, request: InteractionRequest) -> JsonObject:
        if self.interaction_handler is not None:
            return await self.interaction_handler(request)
        reason = "This ZCode client has no human interaction handler."
        if isinstance(request, PermissionRequest):
            return {"decision": "deny", "reason": reason}
        return {"action": "decline", "reason": reason}

    async def reply_interaction(
        self, rpc_id: int | str, task: asyncio.Task[JsonObject]
    ) -> None:
        try:
            result = await asyncio.shield(task)
            await self.send({"id": rpc_id, "result": result})
        except Exception as error:
            message = (
                "Invalid ZCode interaction payload"
                if isinstance(error, ValidationError)
                else self.redact(str(error))
            )
            self.fail(AgentRunError(f"ZCode interaction failed: {message}"))
            with contextlib.suppress(AgentRunError, OSError):
                await self.send(
                    {"id": rpc_id, "error": {"code": -32603, "message": message}}
                )
