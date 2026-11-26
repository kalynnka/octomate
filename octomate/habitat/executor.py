"""Execute one workspace operation in the guest, cancelling it on supervisor EOF."""

import asyncio
import os
import sys
from typing import Annotated, Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_ai.workspaces import (
    CommandResult,
    FileEntry,
    LocalWorkspaceBackend,
    WorkspaceOutputLimitError,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)

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

request_adapter = TypeAdapter(Request)
response_adapter = TypeAdapter(Response)
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


async def execute(request: Request) -> Response:
    # Upstream inherits PATH/HOME, but commands also need the habitat's temp and
    # proxy settings. These come from the guest environment, never core's env.
    backend = LocalWorkspaceBackend(
        ".",
        env={name: os.environ[name] for name in EXECUTE_ENV if name in os.environ},
    )
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
                    )
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


async def send_response(response: Response) -> None:
    """Drain a complete response even when stdin and stdout share a nonblocking socket."""
    payload = response_adapter.dump_json(response)
    pending = memoryview(str(len(payload)).encode() + b"\n" + payload)
    while pending:
        await anyio.wait_writable(sys.stdout.fileno())
        pending = pending[os.write(sys.stdout.fileno(), pending) :]


async def run() -> None:
    reader = asyncio.StreamReader()
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    try:
        size = int(await reader.readline())
        request = request_adapter.validate_json(await reader.readexactly(size))
        operation = asyncio.create_task(execute(request))
        disconnected = asyncio.create_task(reader.read())
        try:
            await asyncio.wait(
                (operation, disconnected), return_when=asyncio.FIRST_COMPLETED
            )
            if operation.done():
                await send_response(operation.result())
        finally:
            operation.cancel()
            disconnected.cancel()
            await asyncio.gather(operation, disconnected, return_exceptions=True)
    finally:
        transport.close()


if __name__ == "__main__":
    asyncio.run(run())
