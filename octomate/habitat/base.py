"""Shared habitat lifecycle and managed runtime capabilities."""

import asyncio
import math
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import ClassVar

from anyio.abc import ByteStream
from octomate_protocol.config import HabitatType
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
from octomate.habitat.client import ExecutorClient
from octomate.habitat.executor import (
    CommandEvent,
    CommandRequest,
    CommandResponse,
    FileError,
    OperationError,
    Request,
    ValueErrorResponse,
    file_errors,
)
from octomate.schemas.volumes import Volume


class HabitatError(RuntimeError):
    """An actionable failure safe to publish without guest or credential output."""


@dataclass(kw_only=True)
class Habitat(ABC):
    """The execution environment core prepares for an agent invocation."""

    type: ClassVar[HabitatType]
    os_sandbox: ClassVar[bool] = (
        True  # Whether harnesses should install their own OS sandbox.
    )
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


@dataclass(kw_only=True)
class ManagedHabitat(Habitat):
    """One registered user's storage and managed execution lifetime."""

    user_id: UUID7  # The registered user whose private runtime this is.
    root: Path  # Owner storage and the executor's startup directory.
    executor: ExecutorClient | None = field(default=None, init=False, repr=False)
    executor_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )

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
        The same primitive carries the retained executor's requests and responses.
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
        self,
        request: Request,
        expected: type[T],
        *,
        workdir: str,
        on_event: Callable[[CommandEvent], None] | None = None,
    ) -> T:
        """Execute an operation; streamed commands deliver output only to on_event."""
        async with self.executor_lock:
            if self.executor is None or self.executor.closed:
                self.executor = ExecutorClient(self)
                self.resources.push_async_callback(self.executor.close)
            client = self.executor
        response = await client.request(request, workdir, on_event)
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


@dataclass(kw_only=True)
class WorkspaceHabitat(ManagedHabitat):
    """Owner storage with execution boundaries scoped to individual workspaces."""

    type: ClassVar[HabitatType] = HabitatType.workspace

    @abstractmethod
    def scope(
        self,
        cwd: Path,
        *,
        read_only: bool = False,
        read_paths: tuple[Path, ...] = (),
        state_paths: tuple[Path, ...] = (),
    ) -> AbstractAsyncContextManager["WorkspaceHabitat"]:
        """Prepare a workspace boundary and release its processes on context exit."""

    @abstractmethod
    def environment(self) -> dict[str, str]:
        """Minimal environment for processes within this execution boundary."""

    @abstractmethod
    def command_prefix(self) -> list[str]:
        """Launch a scoped command from a host-side harness.

        Append a JSON object with cwd, read_only and trusted read_paths, then
        the command argv. The launcher owns confinement, stdio and cleanup;
        OCTOMATE_EXECUTE_ENV carries explicit command environment as JSON.
        Startup failures include the marker 'habitat-execution-start:'.
        """


@dataclass(kw_only=True)
class HostedHabitat(ManagedHabitat):
    """An owner's environment capable of hosting complete agent harnesses."""

    type: ClassVar[HabitatType] = HabitatType.hosted

    @abstractmethod
    def publish(self, port: int) -> AbstractAsyncContextManager[str]:
        """Expose a hosted TCP service to core for the owning context."""
