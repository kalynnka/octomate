"""Pydantic AI workspace access to a core-authorized managed habitat invocation."""

import math
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

import anyio
from anyio import to_thread
from anyio.abc import ByteStream
from anyio.streams.buffered import BufferedByteReceiveStream
from pydantic import BaseModel
from pydantic_ai.workspaces import (
    CommandResult,
    FileEntry,
    SupportsCommands,
    SupportsFilesystem,
    SupportsRealpath,
    WorkspaceBackend,
    WorkspaceCommand,
    WorkspaceOutputLimitError,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)

from octomate.habitat import HabitatError, ManagedHabitat
from octomate.managers.habitat import Invocation
from octomate.tentacles.inkling.worker import (
    CommandRequest,
    CommandResponse,
    DirectoryRequest,
    DirectoryResponse,
    DoneResponse,
    ExistsResponse,
    FileError,
    FileRequest,
    FileResponse,
    ListResponse,
    OperationError,
    Request,
    StatResponse,
    ValueErrorResponse,
    WriteRequest,
    file_errors,
    response_adapter,
)


async def finish_guest(stream: ByteStream, output: BufferedByteReceiveStream) -> None:
    """Let EOF cancel and reap the guest command before detaching its transport."""
    with anyio.fail_after(10, shield=True):
        # A completed guest may already have closed its socket.
        with suppress(anyio.BrokenResourceError, OSError):
            await stream.send_eof()
        try:
            while await output.receive():
                pass
        except anyio.EndOfStream:
            pass


class HabitatWorkspaceBackend(
    WorkspaceBackend, SupportsCommands, SupportsFilesystem, SupportsRealpath
):
    """Borrow a prepared habitat; core retains ownership of its lifetime.

    Commands and files use upstream LocalWorkspaceBackend inside the guest.
    A history reference is checked against the authorized invocation, never used
    to select another habitat or working directory.
    """

    habitat: ManagedHabitat
    cwd: str
    env: dict[str, str]
    reference: WorkspaceRef

    def __init__(self, invocation: Invocation, *, ref: WorkspaceRef | None = None):
        if not isinstance(invocation.habitat, ManagedHabitat):
            raise ValueError("Use LocalWorkspaceBackend for native invocations")
        self.habitat = invocation.habitat
        self.cwd = str(invocation.cwd)
        self.env = invocation.environment
        self.reference = WorkspaceRef(
            provider="octomate",
            id=f"{invocation.owner_id}/{invocation.volume.id}",
        )
        if ref is not None and ref != self.ref:
            raise ValueError(
                "Workspace reference does not match the authorized invocation"
            )

    @property
    def ref(self) -> WorkspaceRef:
        return self.reference

    async def working_dir(self) -> str:
        response = await self.request(DirectoryRequest(), DirectoryResponse)
        return response.path

    async def read_bytes(self, path: str) -> bytes:
        response = await self.request(FileRequest(kind="read", path=path), FileResponse)
        return response.data

    async def write_bytes(self, path: str, data: bytes) -> None:
        await self.request(WriteRequest(path=path, data=data), DoneResponse)

    async def stat(self, path: str) -> FileEntry:
        response = await self.request(FileRequest(kind="stat", path=path), StatResponse)
        return response.entry

    async def list_dir(self, path: str) -> list[FileEntry]:
        response = await self.request(FileRequest(kind="list", path=path), ListResponse)
        return response.entries

    async def make_dir(self, path: str) -> None:
        await self.request(FileRequest(kind="mkdir", path=path), DoneResponse)

    async def remove(self, path: str) -> None:
        await self.request(FileRequest(kind="remove", path=path), DoneResponse)

    async def exists(self, path: str) -> bool:
        response = await self.request(
            FileRequest(kind="exists", path=path), ExistsResponse
        )
        return response.exists

    async def realpath(self, path: str) -> str:
        response = await self.request(
            FileRequest(kind="realpath", path=path), DirectoryResponse
        )
        return response.path

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        # Required by upstream SupportsCommands; enforced by the guest backend.
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> CommandResult:
        if isinstance(command, str) != shell:
            raise TypeError("Use shell=True for a string command, shell=False for argv")
        if not command:
            raise ValueError("Command must not be empty")
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("Timeout must be finite and positive")
        response = await self.request(
            CommandRequest(
                command=command if isinstance(command, str) else list(command),
                shell=shell,
                env=self.env | dict(env or {}),
                timeout=timeout,
            ),
            CommandResponse,
        )
        return response.result

    async def request[T: BaseModel](self, request: Request, expected: type[T]) -> T:
        source = await to_thread.run_sync(
            Path(__file__).with_name("worker.py").read_text
        )
        try:
            async with self.habitat.execute(
                "python", "-c", source, workdir=self.cwd
            ) as stream:
                output = BufferedByteReceiveStream(stream)
                try:
                    payload = request.model_dump_json().encode()
                    await stream.send(str(len(payload)).encode() + b"\n" + payload)
                    size = int(await output.receive_until(b"\n", 32))
                    response = response_adapter.validate_json(
                        await output.receive_exactly(size)
                    )
                finally:
                    await finish_guest(stream, output)
        except (
            HabitatError,
            anyio.IncompleteRead,
            anyio.EndOfStream,
            anyio.BrokenResourceError,
        ) as error:
            raise WorkspaceUnavailableError(str(error)) from error
        if isinstance(response, expected):
            return response
        match response:
            case OperationError(kind="timeout") as error:
                raise WorkspaceTimeoutError(
                    error.message, stdout=error.stdout, stderr=error.stderr
                )
            case OperationError(kind="output_limit") as error:
                raise WorkspaceOutputLimitError(
                    error.message,
                    limit=error.limit,
                    stdout=error.stdout,
                    stderr=error.stderr,
                )
            case OperationError() as error:
                raise WorkspaceUnavailableError(error.message)
            case FileError() as error:
                exception = file_errors[error.error_type]
                if error.errno is None:
                    raise exception(error.message)
                raise exception(error.errno, error.message, error.filename)
            case ValueErrorResponse() as error:
                raise ValueError(error.message)
            case _:
                raise RuntimeError("Guest returned an unexpected workspace response")
