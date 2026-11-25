"""Pydantic AI workspace access to a core-authorized managed habitat invocation."""

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.workspaces import (
    CommandResult,
    FileEntry,
    SupportsCommands,
    SupportsFilesystem,
    SupportsRealpath,
    WorkspaceBackend,
    WorkspaceCommand,
    WorkspaceRef,
)

from octomate.habitat import ManagedHabitat
from octomate.habitat.executor import (
    DirectoryRequest,
    DirectoryResponse,
    DoneResponse,
    ExistsResponse,
    FileRequest,
    FileResponse,
    ListResponse,
    StatResponse,
    WriteRequest,
)
from octomate.managers.habitat import Invocation


@dataclass
class HabitatWorkspace[DepsT](AbstractCapability[DepsT]):
    """Resolve workspace history only within the current authorized invocation."""

    invocation: Invocation

    def get_workspace(
        self, ctx: RunContext[DepsT], *, ref: WorkspaceRef | None
    ) -> WorkspaceBackend:
        return HabitatWorkspaceBackend(self.invocation, ref=ref)


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
        response = await self.habitat.request(
            DirectoryRequest(), DirectoryResponse, workdir=self.cwd
        )
        return response.path

    async def read_bytes(self, path: str) -> bytes:
        response = await self.habitat.request(
            FileRequest(kind="read", path=path), FileResponse, workdir=self.cwd
        )
        return response.data

    async def write_bytes(self, path: str, data: bytes) -> None:
        await self.habitat.request(
            WriteRequest(path=path, data=data), DoneResponse, workdir=self.cwd
        )

    async def stat(self, path: str) -> FileEntry:
        response = await self.habitat.request(
            FileRequest(kind="stat", path=path), StatResponse, workdir=self.cwd
        )
        return response.entry

    async def list_dir(self, path: str) -> list[FileEntry]:
        response = await self.habitat.request(
            FileRequest(kind="list", path=path), ListResponse, workdir=self.cwd
        )
        return response.entries

    async def make_dir(self, path: str) -> None:
        await self.habitat.request(
            FileRequest(kind="mkdir", path=path), DoneResponse, workdir=self.cwd
        )

    async def remove(self, path: str) -> None:
        await self.habitat.request(
            FileRequest(kind="remove", path=path), DoneResponse, workdir=self.cwd
        )

    async def exists(self, path: str) -> bool:
        response = await self.habitat.request(
            FileRequest(kind="exists", path=path), ExistsResponse, workdir=self.cwd
        )
        return response.exists

    async def realpath(self, path: str) -> str:
        response = await self.habitat.request(
            FileRequest(kind="realpath", path=path), DirectoryResponse, workdir=self.cwd
        )
        return response.path

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        # Required by upstream SupportsCommands; enforced by the habitat.
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> CommandResult:
        return await self.habitat.run(
            command,
            workdir=self.cwd,
            shell=shell,
            env=self.env | dict(env or {}),
            timeout=timeout,
        )
