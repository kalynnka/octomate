from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, NotRequired

from pydantic import (
    AliasGenerator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    TypeAdapter,
    model_validator,
)
from pydantic.alias_generators import to_camel
from typing_extensions import TypedDict

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import (
    AtData,
    AtSegment,
    ImageSegment,
    MessageSegment,
    ReplySegment,
    Segment,
    TextSegment,
)
from octomate.schemas.user import UserProfile
from octomate.types.json import JsonObject


@dataclass(frozen=True)
class NapcatOutboundMessage:
    segments: list[JsonObject]


class ActionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    status: str = ""
    retcode: int = 0
    data: JsonObject | None = None
    echo: str | None = None
    message: str | None = None
    wording: str | None = None


class NapcatUserProfile(UserProfile):
    model_config = ConfigDict(
        alias_generator=AliasGenerator(validation_alias=to_camel),
        validate_by_alias=True,
        validate_by_name=True,
        extra="ignore",
        coerce_numbers_to_str=True,
        frozen=True,
    )

    name: str = Field(default="", validation_alias="nick")
    nickname: str | None = Field(default=None)
    gender: str | None = Field(default=None, validation_alias="sex")

    uid: str = ""
    qid: str = ""
    uin: str = ""
    long_nick: str = ""
    remark: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: JsonObject) -> JsonObject:
        if isinstance(data, dict):
            # `user_id` is popped, not kept: on the registry schema that name is
            # the owner FK, and the payload's value is the platform id.
            data["channel_user_id"] = (
                data.get("channel_user_id")
                or data.pop("user_id", None)
                or data.get("uin")
                or ""
            )
            if not data.get("nick") and not data.get("name"):
                data["name"] = (
                    data.get("card")
                    or data.get("nickname")
                    or data.get("remark")
                    or data.get("channel_user_id")
                    or ""
                )
            data.setdefault("nickname", data.get("nickname") or data.get("card"))
        return data


class NapcatSender(UserProfile):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    nickname: str | None = None
    card: str = ""
    role: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: JsonObject) -> JsonObject:
        if isinstance(data, dict):
            # OneBot sender payloads carry the platform id as `user_id`; pop it
            # into channel_user_id so the owner-FK field never sees it.
            if "channel_user_id" not in data and (uid := data.pop("user_id", None)):
                data["channel_user_id"] = uid
            if not data.get("name"):
                data["name"] = data.get("card") or data.get("nickname") or ""
        return data


class NapcatAtData(AtData):
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(validation_alias="qq")


class NapcatAtSegment(Segment):
    type: Literal["at"] = "at"
    data: NapcatAtData

    def __str__(self) -> str:
        return f"@{self.data.name or self.data.user_id}"


class FaceData(TypedDict):
    id: str


class RecordData(TypedDict):
    file: str
    url: NotRequired[str | None]


class VideoData(TypedDict):
    file: str
    url: NotRequired[str | None]


class GenericSegment(Segment):
    type: str
    data: JsonObject = Field(default_factory=dict)


class FaceSegment(Segment):
    type: Literal["face"] = "face"
    data: FaceData


class RecordSegment(Segment):
    type: Literal["record"] = "record"
    data: RecordData


class VideoSegment(Segment):
    type: Literal["video"] = "video"
    data: VideoData


def segment_discriminator(raw: JsonObject | Segment) -> str:
    if isinstance(raw, dict):
        segment_type = raw.get("type", "")
    else:
        segment_type = getattr(raw, "type", "")
    if segment_type in {"text", "at", "image", "reply", "face", "record", "video"}:
        return segment_type
    return "__generic__"


NapcatMessageSegment = Annotated[
    Annotated[TextSegment, Tag("text")]
    | Annotated[NapcatAtSegment, Tag("at")]
    | Annotated[ImageSegment, Tag("image")]
    | Annotated[ReplySegment, Tag("reply")]
    | Annotated[FaceSegment, Tag("face")]
    | Annotated[RecordSegment, Tag("record")]
    | Annotated[VideoSegment, Tag("video")]
    | Annotated[GenericSegment, Tag("__generic__")],
    Discriminator(segment_discriminator),
]


class NapcatMessageEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    post_type: Literal["message"] = "message"
    message_type: Literal["private", "group"]
    time: int = 0
    self_id: str = ""
    message_id: str
    user_id: str
    group_id: str = ""
    sender: NapcatSender = Field(default_factory=NapcatSender)
    message: list[NapcatMessageSegment] = Field(default_factory=list)
    raw_message: str = ""


def _inbound_discriminator(raw: JsonObject) -> str:
    if isinstance(raw, dict) and raw.get("post_type") == "message":
        return "message"
    return "response"


InboundFrame = Annotated[
    Annotated[NapcatMessageEvent, Tag("message")]
    | Annotated[ActionResponse, Tag("response")],
    Discriminator(_inbound_discriminator),
]

inbound_adapter = TypeAdapter(InboundFrame)

COMPATIBLE_SEGMENTS = (TextSegment, AtSegment, ImageSegment, ReplySegment)


def to_message_event(event: NapcatMessageEvent) -> MessageEvent:
    segments: list[MessageSegment] = []
    for seg in event.message:
        if isinstance(seg, NapcatAtSegment):
            segments.append(
                AtSegment(data=AtData(user_id=seg.data.user_id, name=seg.data.name))
            )
        elif isinstance(seg, COMPATIBLE_SEGMENTS):
            segments.append(seg)
    reply_id = next(
        (
            str(seg.data["id"])
            for seg in segments
            if isinstance(seg, ReplySegment) and seg.data.get("id")
        ),
        "",
    )
    chat_type: Literal["private", "group"] = (
        "group" if event.message_type == "group" else "private"
    )
    chat_id = event.group_id if chat_type == "group" else event.user_id
    return MessageEvent(
        message_id=event.message_id,
        reply_id=reply_id,
        timestamp=float(event.time),
        user_id=event.user_id,
        chat_id=chat_id,
        chat_type=chat_type,
        self_id=event.self_id,
        sender=event.sender,
        segments=segments,
        raw=event.raw_message,
    )
