"""ACP v1 client operations confined to the active invocation's workspace."""

import asyncio
import codecs
import signal
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import acp
import anyio
from acp import schema
from pydantic import JsonValue
from uuid_utils.compat import uuid7

from octomate.habitat import Habitat, ManagedHabitat, WorkspaceHabitat
from octomate.habitat.executor import (
    CommandEvent,
    CommandRequest,
    CommandResponse,
    CommandStarted,
    DirectoryResponse,
    DoneResponse,
    FileRequest,
    FileResponse,
    WriteRequest,
)
from octomate.tentacles.acp.adapter import json_value_adapter


@dataclass
class Terminal:
    habitat: ManagedHabitat
    cwd: str
    request: schema.CreateTerminalRequest
    output: str = ""
    truncated: bool = False
    status: schema.TerminalExitStatus | None = None
    failure: Exception | None = None
    stopping: bool = False
    started: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] = field(init=False)

    def append(self, text: str) -> None:
        limit = self.request.output_byte_limit
        limit = 1024 * 1024 if limit is None else limit
        data = (self.output + text).encode()
        if len(data) > limit:
            self.truncated = True
            data = data[-limit:] if limit else b""
        self.output = data.decode("utf-8", errors="ignore")

    async def run(self) -> None:
        decoders = {
            name: codecs.getincrementaldecoder("utf-8")(errors="replace")
            for name in ("stdout", "stderr")
        }

        def receive(event: CommandEvent) -> None:
            if isinstance(event, CommandStarted):
                self.started.set()
            else:
                self.append(decoders[event.stream].decode(event.data))

        try:
            response = await self.habitat.request(
                CommandRequest(
                    command=[self.request.command, *(self.request.args or [])],
                    env={entry.name: entry.value for entry in self.request.env or []},
                ),
                CommandResponse,
                workdir=self.cwd,
                on_event=receive,
            )
            if not self.started.is_set():
                raise RuntimeError(response.result.stderr)
            self.status = schema.TerminalExitStatus(
                signal=signal.Signals(response.termination_signal).name
                if response.termination_signal is not None
                else None,
                exit_code=response.result.exit_code
                if response.termination_signal is None
                else None,
            )
        except asyncio.CancelledError:
            # Cancellation waits for the executor to reap the command.
            self.status = schema.TerminalExitStatus()
        except Exception as error:
            self.failure = error
        finally:
            for decoder in decoders.values():
                self.append(decoder.decode(b"", final=True))
            self.started.set()

    def check(self) -> None:
        if self.failure is not None:
            raise self.failure

    async def kill(self) -> None:
        if not self.task.done() and self.status is None and not self.stopping:
            self.stopping = True
            self.task.cancel()
        await asyncio.shield(self.task)


@dataclass
class AcpWorkspace:
    habitat: ManagedHabitat
    cwd: str
    session_id: str | None = None
    terminals: dict[str, Terminal] = field(default_factory=dict)

    async def path(self, path: str) -> str:
        if not PurePosixPath(path).is_absolute():
            raise acp.RequestError.invalid_params(
                {"reason": "Workspace paths must be absolute"}
            )
        resolved = await self.habitat.request(
            FileRequest(kind="realpath", path=path), DirectoryResponse, workdir=self.cwd
        )
        if not PurePosixPath(resolved.path).is_relative_to(self.cwd):
            raise acp.RequestError.invalid_params(
                {"reason": "Path is outside the session workspace"}
            )
        return resolved.path

    async def handle(self, method: str, params: JsonValue | None) -> JsonValue:
        request: (
            schema.ReadTextFileRequest
            | schema.WriteTextFileRequest
            | schema.CreateTerminalRequest
            | schema.TerminalOutputRequest
            | schema.WaitForTerminalExitRequest
            | schema.KillTerminalRequest
            | schema.ReleaseTerminalRequest
        )
        match method:
            case "fs/read_text_file":
                request = schema.ReadTextFileRequest.model_validate(params)
            case "fs/write_text_file":
                request = schema.WriteTextFileRequest.model_validate(params)
            case "terminal/create":
                request = schema.CreateTerminalRequest.model_validate(params)
            case "terminal/output":
                request = schema.TerminalOutputRequest.model_validate(params)
            case "terminal/wait_for_exit":
                request = schema.WaitForTerminalExitRequest.model_validate(params)
            case "terminal/kill":
                request = schema.KillTerminalRequest.model_validate(params)
            case "terminal/release":
                request = schema.ReleaseTerminalRequest.model_validate(params)
            case _:
                raise acp.RequestError.method_not_found(method)
        if self.session_id is None or request.session_id != self.session_id:
            raise acp.RequestError.invalid_params({"reason": "Unknown session"})
        match request:
            case schema.ReadTextFileRequest():
                if request.line == 0:
                    raise acp.RequestError.invalid_params(
                        {"reason": "Line numbers start at one"}
                    )
                result = await self.habitat.request(
                    FileRequest(kind="read", path=await self.path(request.path)),
                    FileResponse,
                    workdir=self.cwd,
                )
                text = result.data.decode("utf-8")
                start = (request.line or 1) - 1
                lines = text.splitlines(keepends=True)
                return {
                    "content": "".join(
                        lines[
                            start : None
                            if request.limit is None
                            else start + request.limit
                        ]
                    )
                }
            case schema.WriteTextFileRequest():
                await self.habitat.request(
                    WriteRequest(
                        path=await self.path(request.path),
                        data=request.content.encode(),
                    ),
                    DoneResponse,
                    workdir=self.cwd,
                )
                return {}
            case schema.CreateTerminalRequest():
                terminal = Terminal(
                    self.habitat,
                    await self.path(request.cwd or self.cwd),
                    request,
                )
                terminal_id = str(uuid7())
                self.terminals[terminal_id] = terminal
                terminal.task = asyncio.create_task(terminal.run())
                await terminal.started.wait()
                terminal.check()
                return {"terminalId": terminal_id}
        terminal = self.terminals.get(request.terminal_id)
        if terminal is None:
            raise acp.RequestError.invalid_params({"reason": "Unknown terminal"})
        match request:
            case schema.ReleaseTerminalRequest():
                await terminal.kill()
                del self.terminals[request.terminal_id]
                return {}
            case schema.KillTerminalRequest():
                await terminal.kill()
                terminal.check()
                return {}
            case schema.WaitForTerminalExitRequest():
                await asyncio.shield(terminal.task)
                terminal.check()
                if terminal.status is None:
                    raise RuntimeError("Terminal stopped without exit status")
                return json_value_adapter.validate_python(
                    terminal.status.model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    )
                )
            case schema.TerminalOutputRequest():
                terminal.check()
                return json_value_adapter.validate_python(
                    schema.TerminalOutputResponse(
                        output=terminal.output,
                        truncated=terminal.truncated,
                        exit_status=terminal.status,
                    ).model_dump(mode="json", by_alias=True, exclude_none=True)
                )
        raise acp.RequestError.method_not_found(method)

    async def close(self) -> None:
        with anyio.CancelScope(shield=True):
            await asyncio.gather(
                *(terminal.kill() for terminal in self.terminals.values())
            )
        self.terminals.clear()


@asynccontextmanager
async def workspace(habitat: Habitat, cwd: str) -> AsyncGenerator[AcpWorkspace | None]:
    if not isinstance(habitat, WorkspaceHabitat):
        yield None
        return
    async with habitat.scope(Path(cwd)) as scoped:
        client = AcpWorkspace(scoped, str(scoped.root))
        try:
            yield client
        finally:
            await client.close()
