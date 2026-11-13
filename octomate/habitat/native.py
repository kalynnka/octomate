"""Host execution under the selected harness's own controls."""

from octomate.habitat.base import Habitat


class NativeHabitat(Habitat):
    """No separate runtime to acquire; adapters own harness policy and clients."""

    @property
    def name(self) -> str:
        return "native"

    async def prepare(self) -> None:
        return

    async def stop(self) -> None:
        return
