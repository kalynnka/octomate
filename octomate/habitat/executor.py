"""Serve concurrent workspace operations, cancelling children on supervisor EOF."""

import asyncio
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Annotated, Literal

import anyio
from anyio.abc import ByteReceiveStream, Process
from pydantic import UUID7, BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_ai.workspaces import (
    CommandResult,
    FileEntry,
    LocalWorkspaceBackend,
    WorkspaceOutputLimitError,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)
from pydantic_ai.workspaces.local import _OUTPUT_DRAIN_GRACE

EXECUTE_ENV: tuple[str, ...] = (
    "TMPDIR",
    "TMP",
    "TEMP",
    "PYTHONDONTWRITEBYTECODE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


class CommandRequest(BaseModel):
    kind: Literal["command"] = "command"
    command: str | list[str]
    shell: bool = False
    env: dict[str, str] = Field(default_factory=dict)
    timeout: float | None = None


class DirectoryRequest(BaseModel):
    kind: Literal["directory"] = "directory"


class FileRequest(BaseModel):
    kind: Literal["read", "stat", "list", "mkdir", "remove", "exists", "realpath"]
    path: str


class WriteRequest(BaseModel):
    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")
    kind: Literal["write"] = "write"
    path: str
    data: bytes


class CommandResponse(BaseModel):
    kind: Literal["command"] = "command"
    result: CommandResult
    termination_signal: int | None = None


class CommandStarted(BaseModel):
    kind: Literal["started"] = "started"


class CommandOutput(BaseModel):
    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")
    kind: Literal["output"] = "output"
    stream: Literal["stdout", "stderr"]
    data: bytes


type CommandEvent = CommandStarted | CommandOutput


class DirectoryResponse(BaseModel):
    kind: Literal["directory"] = "directory"
    path: str


class FileResponse(BaseModel):
    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")
    kind: Literal["file"] = "file"
    data: bytes


class StatResponse(BaseModel):
    kind: Literal["stat"] = "stat"
    entry: FileEntry


class ListResponse(BaseModel):
    kind: Literal["list"] = "list"
    entries: list[FileEntry]


class ExistsResponse(BaseModel):
    kind: Literal["exists"] = "exists"
    exists: bool


class DoneResponse(BaseModel):
    kind: Literal["done"] = "done"


class FileError(BaseModel):
    kind: Literal["file_error"] = "file_error"
    error_type: Literal[
        "FileNotFoundError",
        "IsADirectoryError",
        "NotADirectoryError",
        "PermissionError",
        "FileExistsError",
        "OSError",
    ]
    errno: int | None
    message: str
    filename: str | None


class ValueErrorResponse(BaseModel):
    kind: Literal["value_error"] = "value_error"
    message: str


class OperationError(BaseModel):
    kind: Literal["timeout", "output_limit", "unavailable"]
    message: str
    stdout: str = ""
    stderr: str = ""
    limit: int = 0


type Request = Annotated[
    CommandRequest | DirectoryRequest | FileRequest | WriteRequest,
    Field(discriminator="kind"),
]
type Response = Annotated[
    CommandResponse
    | DirectoryResponse
    | OperationError
    | FileResponse
    | StatResponse
    | ListResponse
    | ExistsResponse
    | DoneResponse
    | FileError
    | ValueErrorResponse,
    Field(discriminator="kind"),
]


class ExecuteRequest(BaseModel):
    kind: Literal["execute"] = "execute"
    id: UUID7
    workdir: str
    request: Request
    stream_output: bool = False


class CancelRequest(BaseModel):
    kind: Literal["cancel"] = "cancel"
    id: UUID7


class CancelledResponse(BaseModel):
    kind: Literal["cancelled"] = "cancelled"


class ExecutionResponse(BaseModel):
    id: UUID7
    response: Response | CancelledResponse | CommandEvent


type ExecutionRequest = Annotated[
    ExecuteRequest | CancelRequest, Field(discriminator="kind")
]
execution_request_adapter = TypeAdapter(ExecutionRequest)
file_errors: dict[str, type[OSError]] = {
    cls.__name__: cls
    for cls in (
        FileNotFoundError,
        IsADirectoryError,
        NotADirectoryError,
        PermissionError,
        FileExistsError,
        OSError,
    )
}


class CommandWorkspace(LocalWorkspaceBackend):
    """Commands finish with their process group; residents have a separate lifetime."""

    on_event: Callable[[CommandEvent], Awaitable[None]] | None = None
    termination_signal: int | None = None

    async def _wait_and_collect_output(
        self,
        process: Process,
        stdout_buffer: bytearray,
        stderr_buffer: bytearray,
        absolute_deadline: float | None,
    ) -> int:
        if self.on_event is None:
            return await super()._wait_and_collect_output(
                process, stdout_buffer, stderr_buffer, absolute_deadline
            )
        # Keep upstream's spawn, timeout and termination lifecycle. Streaming
        # callers retain their own output instead of collecting it twice here.
        emit = self.on_event
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("Command output pipes are unavailable")

        async def collect(
            pipe: ByteReceiveStream, name: Literal["stdout", "stderr"]
        ) -> None:
            async for chunk in pipe:
                await emit(CommandOutput(stream=name, data=chunk))

        remaining = (
            None
            if absolute_deadline is None
            else absolute_deadline - anyio.current_time()
        )
        with anyio.fail_after(remaining):
            await emit(CommandStarted())
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(collect, process.stdout, "stdout")
                tasks.start_soon(collect, process.stderr, "stderr")
                exit_code = await self._wait_for_exit(process)
                tasks.cancel_scope.deadline = anyio.current_time() + _OUTPUT_DRAIN_GRACE
        return exit_code

    async def _wait_for_exit(self, process: Process) -> int:
        # Upstream leaves background children alive on successful completion.
        # Keep its cancellation cleanup; reap remaining children on ordinary exit.
        exit_code = await super()._wait_for_exit(process)
        if process.returncode is not None and process.returncode < 0:
            self.termination_signal = -process.returncode
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        return exit_code


async def execute(
    request: Request,
    workdir: str = ".",
    on_event: Callable[[CommandEvent], Awaitable[None]] | None = None,
) -> Response:
    # Upstream inherits PATH/HOME, but commands also need the habitat's temp and
    # proxy settings. These come from the guest environment, never core's env.
    backend = CommandWorkspace(
        workdir,
        env={name: os.environ[name] for name in EXECUTE_ENV if name in os.environ},
    )
    backend.on_event = on_event
    try:
        match request:
            case DirectoryRequest():
                return DirectoryResponse(path=await backend.working_dir())
            case CommandRequest():
                return CommandResponse(
                    result=await backend.run(
                        request.command,
                        shell=request.shell,
                        env=request.env,
                        timeout=request.timeout,
                    ),
                    termination_signal=backend.termination_signal,
                )
            case WriteRequest():
                await backend.write_bytes(request.path, request.data)
                return DoneResponse()
            case FileRequest(kind="read"):
                return FileResponse(data=await backend.read_bytes(request.path))
            case FileRequest(kind="stat"):
                return StatResponse(entry=await backend.stat(request.path))
            case FileRequest(kind="list"):
                return ListResponse(entries=list(await backend.list_dir(request.path)))
            case FileRequest(kind="exists"):
                return ExistsResponse(exists=await backend.exists(request.path))
            case FileRequest(kind="realpath"):
                return DirectoryResponse(path=await backend.realpath(request.path))
            case FileRequest(kind="mkdir"):
                await backend.make_dir(request.path)
                return DoneResponse()
            case FileRequest(kind="remove"):
                await backend.remove(request.path)
                return DoneResponse()
        raise ValueError("Unknown workspace operation")
    except WorkspaceTimeoutError as error:
        return OperationError(
            kind="timeout", message=str(error), stdout=error.stdout, stderr=error.stderr
        )
    except WorkspaceOutputLimitError as error:
        return OperationError(
            kind="output_limit",
            message=str(error),
            stdout=error.stdout,
            stderr=error.stderr,
            limit=error.limit,
        )
    except WorkspaceUnavailableError as error:
        return OperationError(kind="unavailable", message=str(error))
    except OSError as error:
        return FileError.model_validate(
            {
                "error_type": next(
                    name for name, cls in file_errors.items() if isinstance(error, cls)
                ),
                "errno": error.errno,
                "message": str(error),
                "filename": error.filename,
            }
        )
    except ValueError as error:
        return ValueErrorResponse(message=str(error))


async def send_response(response: ExecutionResponse) -> None:
    """Drain a complete response even when stdin and stdout share a nonblocking socket."""
    payload = response.model_dump_json().encode()
    pending = memoryview(str(len(payload)).encode() + b"\n" + payload)
    while pending:
        await anyio.wait_writable(sys.stdout.fileno())
        pending = pending[os.write(sys.stdout.fileno(), pending) :]


async def run() -> None:
    reader = asyncio.StreamReader()
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    pending: dict[UUID7, asyncio.Task[Response]] = {}
    writing = asyncio.Lock()
    closing = False

    async def emit(
        key: UUID7, response: Response | CancelledResponse | CommandEvent
    ) -> None:
        if not closing:
            async with writing:
                # A cancelled command must not leave a partial frame on the
                # connection used by other operations.
                sending = asyncio.create_task(
                    send_response(ExecutionResponse(id=key, response=response))
                )
                try:
                    await asyncio.shield(sending)
                except asyncio.CancelledError:
                    with anyio.CancelScope(shield=True):
                        await sending
                    raise

    async def serve(message: ExecuteRequest, operation: asyncio.Task[Response]) -> None:
        try:
            response: Response | CancelledResponse = await operation
        except asyncio.CancelledError:
            response = CancelledResponse()
        await emit(message.id, response)

    async def dispatch(tasks: asyncio.TaskGroup) -> None:
        while size := await reader.readline():
            message = execution_request_adapter.validate_json(
                await reader.readexactly(int(size))
            )
            if isinstance(message, CancelRequest):
                if task := pending.get(message.id):
                    task.cancel()
                continue
            if message.id in pending:
                raise ValueError("Duplicate executor request ID")
            pending[message.id] = tasks.create_task(
                execute(
                    message.request,
                    message.workdir,
                    (lambda event, key=message.id: emit(key, event))
                    if message.stream_output
                    else None,
                )
            )
            reply = tasks.create_task(serve(message, pending[message.id]))
            reply.add_done_callback(lambda _, key=message.id: pending.pop(key, None))

    try:
        print("ready", flush=True)
        async with asyncio.TaskGroup() as tasks:
            try:
                await dispatch(tasks)
            finally:
                closing = True
                for task in pending.values():
                    task.cancel()
    finally:
        transport.close()


if __name__ == "__main__":
    asyncio.run(run())
