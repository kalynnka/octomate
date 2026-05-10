"""Stub `Chromo` for the DevUI tentacle.

`sip` is unused: DevUI bypasses `ChannelTentacle.ingest` because the FastAPI
endpoint is its own inbound dispatch. Defined to satisfy the Chromo protocol
and raise loudly if anything ever invokes it.
"""

from typing import Any

from octomate.schemas.events import MessageEvent


class StubChromo:
    async def sip(self, raw: Any) -> MessageEvent | None:
        raise NotImplementedError(
            "DevUI tentacle bypasses Chromo.sip — inbound goes through /api/chat"
        )
