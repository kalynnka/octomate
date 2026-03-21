from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import AgentSegment


@dataclass
class PlatformMessage:
    msg_type: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Chromo(Protocol):
    """Two-way translation between platform-native wire format and internal schema.

    decode: platform payload → MessageEvent (inbound)
    encode: agent segments → PlatformMessage list (outbound)
    """

    async def decode(self, raw: Any) -> MessageEvent | None: ...

    async def encode(
        self, segments: list[AgentSegment], *, reply_to: str | None = None
    ) -> list[PlatformMessage]: ...
