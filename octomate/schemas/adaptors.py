"""Pre-built Pydantic TypeAdapters for common unions."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Discriminator, Tag, TypeAdapter

from octomate.schemas.events import (
    ActionResponse,
    OneBotEvent,
    OneBotEventUnion,
)


def inbound_discriminator(raw: Any) -> str:
    if isinstance(raw, dict) and "post_type" in raw:
        return "event"
    if isinstance(raw, OneBotEvent):
        return "event"
    return "response"


InboundFrame = Annotated[
    Annotated[OneBotEventUnion, Tag("event")]
    | Annotated[ActionResponse, Tag("response")],
    Discriminator(inbound_discriminator),
]

inbound_adapter: TypeAdapter[InboundFrame] = TypeAdapter(InboundFrame)
