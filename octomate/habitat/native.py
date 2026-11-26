"""Host execution under the selected harness's own controls."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import ClassVar

from pydantic_ai.workspaces import (
    CommandResult,
    LocalWorkspaceBackend,
    WorkspaceCommand,
)
from uuid_utils.compat import uuid7

from octomate.config.habitat import NativeHabitatConfig
from octomate.habitat.base import Habitat
from octomate.schemas.volumes import Volume


@dataclass
class NativeHabitat(Habitat):
    """No separate runtime to acquire; adapters own harness policy and clients."""

    type: ClassVar[str] = "native"
    config: NativeHabitatConfig = field(default_factory=NativeHabitatConfig)

    @cached_property
    def id(self) -> str:
        return f"{self.type}-{uuid7().hex}"

    def path(self, volume: Volume) -> Path:
        return volume.path

    async def prepare(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        workdir: str,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        # Match the upstream command contract; LocalWorkspaceBackend enforces it.
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> CommandResult:
        if not command:
            raise ValueError("Command must not be empty")
        return await LocalWorkspaceBackend(workdir).run(
            command, shell=shell, env=env, timeout=timeout
        )
