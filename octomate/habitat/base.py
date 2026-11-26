"""Shared habitat lifecycle and managed runtime capabilities."""

import math
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import ClassVar

import anyio
from anyio import to_thread
from anyio.abc import ByteStream
from anyio.streams.buffered import BufferedByteReceiveStream
from pydantic import UUID7, BaseModel
from pydantic_ai.workspaces import (
    CommandResult,
    WorkspaceCommand,
    WorkspaceOutputLimitError,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)
from yarl import URL

from octomate.config.habitat import BaseHabitatConfig
from octomate.habitat.executor import (
    CommandRequest,
    CommandResponse,
    FileError,
    OperationError,
    Request,
    ValueErrorResponse,
    file_errors,
    response_adapter,
)
from octomate.schemas.volumes import Volume


class HabitatError(RuntimeError):
    """An actionable failure safe to publish without guest or credential output."""


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


@dataclass(kw_only=True)
class Habitat(ABC):
    """The execution environment core prepares for an agent invocation."""

    type: ClassVar[str]  # Backend kind, independent of configured names and owners.
    config: BaseHabitatConfig  # This environment's settings, including core access.
    resources: AsyncExitStack = field(
        default_factory=AsyncExitStack, init=False, repr=False, compare=False
    )  # Clients released by core before stopping this environment.

    @property
    def core_url(self) -> str:
        """Core's HTTP origin as reached from this runtime."""
        address = URL(str(self.config.core_address)).origin()
        match address.host:
            case "0.0.0.0":
                return str(address.with_host("127.0.0.1"))
            case "::":
                return str(address.with_host("::1"))
        return str(address)

    @property
    @abstractmethod
    def id(self) -> str:
        """Stable runtime identifier used for process routing and lifecycle operations."""

    @abstractmethod
    def path(self, volume: Volume) -> Path | PurePosixPath:
        """The volume location visible to the harness in this environment."""

    @abstractmethod
    async def prepare(self) -> None:
        """Make the environment ready before an adapter runs in it."""

    @abstractmethod
    async def stop(self) -> None:
        """Release the environment's runtime resources, preserving persistent data."""

    @abstractmethod
    async def run(
        self,
        command: WorkspaceCommand,
        *,
        workdir: str,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        # Match the upstream command contract; the execution backend enforces it.
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> CommandResult:
        """Run to completion with stdin closed and capture text stdout and stderr.

        Use argv by default, or a string with shell=True. Nonzero exits are results.
        Timeout and output-limit errors retain partial output; cancellation reaps
        the foreground process tree. Commands inherit only the backend's minimal
        environment plus explicit env values. workdir is a path inside this habitat.
        """


class ManagedHabitat(Habitat):
    """One registered user's persistent runtime, hosting agent processes."""

    user_id: UUID7  # The registered user whose private runtime this is.

    def path(self, volume: Volume) -> PurePosixPath:
        raise HabitatError("Managed habitat volume mounts are not yet supported")

    @property
    @abstractmethod
    def idle_timeout(self) -> float:
        """Seconds to keep the runtime after its last invocation ends."""

    @abstractmethod
    def resident(
        self,
        *command: str,
        workdir: str,
        env: dict[str, str] | None = None,
        stderr: Callable[[str], None] | None = None,
    ) -> AbstractAsyncContextManager[ByteStream]:
        """Keep a process attached through duplex stdio for the owning context.

        SDK clients and service runners keep this context open for their lifetime.
        The same primitive carries the short-lived executor's request and response.
        Deliver stderr lines on the caller's event loop; close and reap the process
        on context exit, reporting an unsuccessful exit.
        """

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        workdir: str,
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
                env=dict(env or {}),
                timeout=timeout,
            ),
            CommandResponse,
            workdir=workdir,
        )
        return response.result

    async def request[T: BaseModel](
        self, request: Request, expected: type[T], *, workdir: str
    ) -> T:
        source = await to_thread.run_sync(
            Path(__file__).with_name("executor.py").read_text
        )
        try:
            async with self.resident("python", "-c", source, workdir=workdir) as stream:
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
