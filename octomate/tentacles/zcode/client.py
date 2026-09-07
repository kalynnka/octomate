from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from pathlib import Path
from types import TracebackType

import anyio
from pydantic import SecretStr, ValidationError
from pydantic_ai.exceptions import AgentRunError

from octomate.tentacles.zcode.wire import (
    RpcNotification,
    RpcRequest,
    RpcResponse,
    SessionEvent,
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
    ) -> None:
        self.command: list[str] = command
        self.cwd: Path = cwd
        self.state_dir: Path = state_dir
        self.request_timeout: float = request_timeout
        self.secrets: list[SecretStr] = secrets
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
            self.closing = True
            process = self.process
            if process is not None:
                # The runtime may own tool subprocesses. Its process group belongs to this run.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            for task in (self.reader, self.stderr_reader):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
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
                    if request.method == "interaction/requestPermission":
                        result: JsonObject = {
                            "decision": "deny",
                            "reason": "Octomate's ZCode runner does not support approval cards yet.",
                        }
                    elif request.method == "interaction/requestUserInput":
                        result = {
                            "action": "decline",
                            "reason": "Octomate's ZCode runner does not support question cards yet.",
                        }
                    elif request.method == "session/requestRuntimePreferences":
                        result = {
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
        self.failure = error
        for future in self.pending.values():
            if not future.done():
                future.set_exception(error)
        self.events.put_nowait(error)
