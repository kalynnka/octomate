"""Napcat-specific schema overrides.

Napcat sends ``{"qq": "..."}`` instead of ``{"user_id": "..."}`` in the
at-segment data payload.  We subclass the standard AtData / AtSegment so
the inbound adapter can parse both forms while the rest of the codebase
keeps using the platform-agnostic ``user_id`` attribute.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import ConfigDict, Discriminator, Field, Tag, TypeAdapter

from octomate.schemas.actions import ActionResponse
from octomate.schemas.events import (
    Event,
    GroupMessageEvent,
    MetaEventUnion,
    NoticeEventUnion,
    PrivateMessageEvent,
    RequestEventUnion,
)
from octomate.schemas.segments import (
    AnonymousSegment,
    AtData,
    AtSegment,
    ContactSegment,
    DiceSegment,
    FaceSegment,
    ForwardSegment,
    ImageSegment,
    JsonSegment,
    LocationSegment,
    MusicSegment,
    NodeSegment,
    PokeSegment,
    RecordSegment,
    ReplySegment,
    RpsSegment,
    ShakeSegment,
    ShareSegment,
    TextSegment,
    VideoSegment,
    XmlSegment,
)


class NapcatAtData(AtData):
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(validation_alias="qq")


class NapcatAtSegment(AtSegment):
    type: Literal["at"] = "at"
    data: NapcatAtData

    def __str__(self) -> str:
        return f"@{self.data.name or self.data.user_id}"


NapcatMessageSegment = Annotated[
    Union[
        TextSegment,
        NapcatAtSegment,
        ImageSegment,
        ReplySegment,
        FaceSegment,
        RecordSegment,
        VideoSegment,
        RpsSegment,
        DiceSegment,
        ShakeSegment,
        PokeSegment,
        AnonymousSegment,
        ShareSegment,
        ContactSegment,
        LocationSegment,
        MusicSegment,
        ForwardSegment,
        NodeSegment,
        XmlSegment,
        JsonSegment,
    ],
    Discriminator("type"),
]


class NapcatGroupMessageEvent(GroupMessageEvent):
    message: list[NapcatMessageSegment] = Field(default_factory=list)


class NapcatPrivateMessageEvent(PrivateMessageEvent):
    message: list[NapcatMessageSegment] = Field(default_factory=list)


NapcatMessageEventUnion = Annotated[
    NapcatGroupMessageEvent | NapcatPrivateMessageEvent,
    Discriminator("message_type"),
]

NapcatEventUnion = Annotated[
    Annotated[NapcatMessageEventUnion, Tag("message")]
    | Annotated[NoticeEventUnion, Tag("notice")]
    | Annotated[RequestEventUnion, Tag("request")]
    | Annotated[MetaEventUnion, Tag("meta_event")],
    Discriminator("post_type"),
]


def inbound_discriminator(raw: Any) -> str:
    if isinstance(raw, dict) and "post_type" in raw:
        return "event"
    if isinstance(raw, Event):
        return "event"
    return "response"


InboundFrame = Annotated[
    Annotated[NapcatEventUnion, Tag("event")]
    | Annotated[ActionResponse, Tag("response")],
    Discriminator(inbound_discriminator),
]

inbound_adapter: TypeAdapter[InboundFrame] = TypeAdapter(InboundFrame)
