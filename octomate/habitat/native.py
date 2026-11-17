"""Host execution under the selected harness's own controls."""

from dataclasses import dataclass, field
from pathlib import Path

from octomate.config.habitat import NativeHabitatConfig
from octomate.habitat.base import Habitat
from octomate.schemas.volumes import Volume


@dataclass
class NativeHabitat(Habitat):
    """No separate runtime to acquire; adapters own harness policy and clients."""

    config: NativeHabitatConfig = field(default_factory=NativeHabitatConfig)

    @property
    def name(self) -> str:
        return "native"

    def path(self, volume: Volume) -> Path:
        return volume.path

    async def prepare(self) -> None:
        return

    async def stop(self) -> None:
        return
