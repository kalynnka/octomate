from __future__ import annotations

from abc import ABC
from functools import cached_property
from pathlib import Path
from typing import Annotated, Any, Literal, NamedTuple, NotRequired, Union

from pydantic import BaseModel, Discriminator, Field, Tag, field_validator
from typing_extensions import TypedDict


class SessionKey(NamedTuple):
    tentacle_id: str
    user_id: int
    group_id: int | None = None


class TextData(TypedDict):
    text: str


class AtData(TypedDict):
    qq: str
    name: NotRequired[str | None]


class ImageData(BaseModel):
    """Kept as BaseModel for the file-path validator."""

    file: str
    url: str | None = None
    name: str | None = None
    summary: str | None = None
    sub_type: int | None = None

    @field_validator("file")
    @classmethod
    def _normalize_file_uri(cls, v: str) -> str:
        if not v.startswith(("http://", "https://", "base64://", "file://")):
            return f"file://{Path(v).resolve()}"
        return v


class ReplyData(TypedDict):
    id: str


class FaceData(TypedDict):
    id: str


class RecordData(TypedDict):
    file: str
    url: NotRequired[str | None]


class VideoData(TypedDict):
    file: str
    url: NotRequired[str | None]


class PokeData(TypedDict):
    type: str
    id: str
    name: NotRequired[str | None]


class AnonymousData(TypedDict, total=False):
    ignore: int | None


class ShareData(TypedDict):
    url: str
    title: str
    content: NotRequired[str | None]
    image: NotRequired[str | None]


class ContactData(TypedDict):
    type: Literal["qq", "group"]
    id: str


class LocationData(TypedDict):
    lat: str
    lon: str
    title: NotRequired[str | None]
    content: NotRequired[str | None]


class MusicData(TypedDict):
    type: Literal["qq", "163", "xm", "custom"]
    id: NotRequired[str | None]
    url: NotRequired[str | None]
    audio: NotRequired[str | None]
    title: NotRequired[str | None]
    content: NotRequired[str | None]
    image: NotRequired[str | None]


class ForwardData(TypedDict):
    id: str


class NodeData(TypedDict, total=False):
    id: str | None
    user_id: str | None
    nickname: str | None
    content: str | None


class XmlData(TypedDict):
    content: str


class JsonData(TypedDict):
    content: str


class Segment(BaseModel):
    """Base class for all message segments."""


class TextSegment(Segment):
    type: Literal["text"] = "text"
    data: TextData


class AtSegment(Segment):
    type: Literal["at"] = "at"
    data: AtData


class ImageSegment(Segment):
    type: Literal["image"] = "image"
    data: ImageData


class ReplySegment(Segment):
    type: Literal["reply"] = "reply"
    data: ReplyData


class FaceSegment(Segment):
    type: Literal["face"] = "face"
    data: FaceData


class RecordSegment(Segment):
    type: Literal["record"] = "record"
    data: RecordData


class VideoSegment(Segment):
    type: Literal["video"] = "video"
    data: VideoData


class RpsSegment(Segment):
    type: Literal["rps"] = "rps"
    data: dict[str, Any] = Field(default_factory=dict)


class DiceSegment(Segment):
    type: Literal["dice"] = "dice"
    data: dict[str, Any] = Field(default_factory=dict)


class ShakeSegment(Segment):
    type: Literal["shake"] = "shake"
    data: dict[str, Any] = Field(default_factory=dict)


class PokeSegment(Segment):
    type: Literal["poke"] = "poke"
    data: PokeData


class AnonymousSegment(Segment):
    type: Literal["anonymous"] = "anonymous"
    data: AnonymousData = Field(default_factory=AnonymousData)


class ShareSegment(Segment):
    type: Literal["share"] = "share"
    data: ShareData


class ContactSegment(Segment):
    type: Literal["contact"] = "contact"
    data: ContactData


class LocationSegment(Segment):
    type: Literal["location"] = "location"
    data: LocationData


class MusicSegment(Segment):
    type: Literal["music"] = "music"
    data: MusicData


class ForwardSegment(Segment):
    type: Literal["forward"] = "forward"
    data: ForwardData


class NodeSegment(Segment):
    type: Literal["node"] = "node"
    data: NodeData


class XmlSegment(Segment):
    type: Literal["xml"] = "xml"
    data: XmlData


class JsonSegment(Segment):
    type: Literal["json"] = "json"
    data: JsonData


MessageSegment = Annotated[
    Union[
        TextSegment,
        AtSegment,
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


class Sender(BaseModel):
    """Sender metadata attached to every message event."""

    user_id: int = 0
    nickname: str = ""
    card: str | None = None
    role: str | None = None
    sex: str | None = None
    age: int | None = None
    area: str | None = None
    level: str | None = None
    title: str | None = None


class Anonymous(BaseModel):
    """Anonymous poster info (group events only)."""

    id: int
    name: str
    flag: str


class ActionResponse(BaseModel):
    """Response received after sending an action via WebSocket."""

    status: str = ""
    retcode: int = 0
    data: dict[str, Any] | None = None
    echo: str | None = None
    message: str | None = None
    wording: str | None = None


class SendGroupMsgParams(BaseModel):
    group_id: int
    message: list[MessageSegment]
    reply: int | None = None


class SendGroupMsgAction(BaseModel):
    action: Literal["send_group_msg"] = "send_group_msg"
    tentacle_id: str
    params: SendGroupMsgParams


class SendPrivateMsgParams(BaseModel):
    user_id: int
    message: list[MessageSegment]
    reply: int | None = None


class SendPrivateMsgAction(BaseModel):
    action: Literal["send_private_msg"] = "send_private_msg"
    tentacle_id: str
    params: SendPrivateMsgParams


class CallApiAction(BaseModel):
    action: str
    tentacle_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class OneBotEvent(BaseModel, ABC):
    time: int = 0
    self_id: int = 0
    tentacle_id: str = ""


class MessageEvent(OneBotEvent):
    post_type: Literal["message"] = "message"
    sub_type: str = "normal"

    message_id: int
    user_id: int
    font: int = 0

    sender: Sender
    message: list[MessageSegment] = Field(default_factory=list)
    raw_message: str = ""

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(tentacle_id=self.tentacle_id, user_id=self.user_id)

    def text_content(self) -> str:
        return "".join(
            seg.data["text"] for seg in self.message if isinstance(seg, TextSegment)
        )


class GroupMessageEvent(MessageEvent):
    message_type: Literal["group"] = "group"

    group_id: int
    anonymous: Anonymous | None = None

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(
            tentacle_id=self.tentacle_id, user_id=self.user_id, group_id=self.group_id
        )

    def is_at(self, qq: int) -> bool:
        target = str(qq)
        return any(
            isinstance(seg, AtSegment) and seg.data["qq"] == target
            for seg in self.message
        )

    def filter_text(self) -> list[str]:
        return [
            seg.data["text"] for seg in self.message if isinstance(seg, TextSegment)
        ]

    @property
    def display_name(self) -> str:
        return self.sender.card or self.sender.nickname or "anonymous"


class PrivateMessageEvent(MessageEvent):
    message_type: Literal["private"] = "private"
    sub_type: str = "friend"

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(tentacle_id=self.tentacle_id, user_id=self.user_id)

    @property
    def display_name(self) -> str:
        return self.sender.nickname or "anonymous"


MessageEventUnion = Annotated[
    GroupMessageEvent | PrivateMessageEvent,
    Discriminator("message_type"),
]


class NoticeEvent(OneBotEvent):
    post_type: Literal["notice"] = "notice"


class GroupNoticeEvent(NoticeEvent):
    group_id: int
    user_id: int

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(
            tentacle_id=self.tentacle_id, user_id=self.user_id, group_id=self.group_id
        )


class GroupUploadNotice(GroupNoticeEvent):
    notice_type: Literal["group_upload"] = "group_upload"
    file: dict[str, Any]


class GroupAdminNotice(GroupNoticeEvent):
    notice_type: Literal["group_admin"] = "group_admin"
    sub_type: Literal["set", "unset"]


class GroupDecreaseNotice(GroupNoticeEvent):
    notice_type: Literal["group_decrease"] = "group_decrease"
    sub_type: Literal["leave", "kick", "kick_me"]
    operator_id: int


class GroupIncreaseNotice(GroupNoticeEvent):
    notice_type: Literal["group_increase"] = "group_increase"
    sub_type: Literal["approve", "invite"]
    operator_id: int


class GroupBanNotice(GroupNoticeEvent):
    notice_type: Literal["group_ban"] = "group_ban"
    sub_type: Literal["ban", "lift_ban"]
    operator_id: int
    duration: int = 0


class GroupRecallNotice(GroupNoticeEvent):
    notice_type: Literal["group_recall"] = "group_recall"
    operator_id: int
    message_id: int


class GroupCardNotice(GroupNoticeEvent):
    notice_type: Literal["group_card"] = "group_card"
    card_new: str = ""
    card_old: str = ""


class GroupEssenceNotice(GroupNoticeEvent):
    notice_type: Literal["group_essence"] = "group_essence"
    sub_type: Literal["add", "delete"]
    operator_id: int
    message_id: int


class FriendAddNotice(NoticeEvent):
    notice_type: Literal["friend_add"] = "friend_add"
    user_id: int

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(tentacle_id=self.tentacle_id, user_id=self.user_id)


class FriendRecallNotice(NoticeEvent):
    notice_type: Literal["friend_recall"] = "friend_recall"
    user_id: int
    message_id: int

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(tentacle_id=self.tentacle_id, user_id=self.user_id)


class GroupPokeNotice(NoticeEvent):
    notice_type: Literal["notify"] = "notify"
    sub_type: Literal["poke"] = "poke"
    group_id: int | None = None
    user_id: int
    target_id: int

    @cached_property
    def session_key(self) -> SessionKey:
        if self.group_id is not None:
            return SessionKey(
                tentacle_id=self.tentacle_id,
                user_id=self.user_id,
                group_id=self.group_id,
            )
        return SessionKey(tentacle_id=self.tentacle_id, user_id=self.user_id)


class GroupLuckyKingNotice(NoticeEvent):
    notice_type: Literal["notify"] = "notify"
    sub_type: Literal["lucky_king"] = "lucky_king"
    group_id: int
    user_id: int
    target_id: int

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(
            tentacle_id=self.tentacle_id, user_id=self.user_id, group_id=self.group_id
        )


class GroupHonorNotice(NoticeEvent):
    notice_type: Literal["notify"] = "notify"
    sub_type: Literal["honor"] = "honor"
    group_id: int
    user_id: int
    honor_type: str

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(
            tentacle_id=self.tentacle_id, user_id=self.user_id, group_id=self.group_id
        )


class MsgEmojiLikeNotice(GroupNoticeEvent):
    notice_type: Literal["group_msg_emoji_like"] = "group_msg_emoji_like"
    message_id: int
    likes: list[dict[str, Any]] = Field(default_factory=list)


NotifyEventUnion = Annotated[
    GroupPokeNotice | GroupLuckyKingNotice | GroupHonorNotice,
    Discriminator("sub_type"),
]

NoticeEventUnion = Annotated[
    Union[
        GroupUploadNotice,
        GroupAdminNotice,
        GroupDecreaseNotice,
        GroupIncreaseNotice,
        GroupBanNotice,
        GroupRecallNotice,
        GroupCardNotice,
        GroupEssenceNotice,
        FriendAddNotice,
        FriendRecallNotice,
        MsgEmojiLikeNotice,
        Annotated[NotifyEventUnion, Tag("notify")],
    ],
    Discriminator("notice_type"),
]


class RequestEvent(OneBotEvent):
    post_type: Literal["request"] = "request"


class FriendRequest(RequestEvent):
    request_type: Literal["friend"] = "friend"
    user_id: int
    comment: str = ""
    flag: str = ""

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(tentacle_id=self.tentacle_id, user_id=self.user_id)


class GroupRequest(RequestEvent):
    request_type: Literal["group"] = "group"
    sub_type: str = "add"
    group_id: int
    user_id: int
    comment: str = ""
    flag: str = ""

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(
            tentacle_id=self.tentacle_id, user_id=self.user_id, group_id=self.group_id
        )


RequestEventUnion = Annotated[
    FriendRequest | GroupRequest,
    Discriminator("request_type"),
]


class MetaEvent(OneBotEvent):
    post_type: Literal["meta_event"] = "meta_event"


class LifecycleEvent(MetaEvent):
    meta_event_type: Literal["lifecycle"] = "lifecycle"
    sub_type: str = ""


class HeartbeatEvent(MetaEvent):
    meta_event_type: Literal["heartbeat"] = "heartbeat"
    status: dict[str, Any] = Field(default_factory=dict)
    interval: int = 0


MetaEventUnion = Annotated[
    LifecycleEvent | HeartbeatEvent,
    Discriminator("meta_event_type"),
]


OneBotEventUnion = Annotated[
    Annotated[MessageEventUnion, Tag("message")]
    | Annotated[NoticeEventUnion, Tag("notice")]
    | Annotated[RequestEventUnion, Tag("request")]
    | Annotated[MetaEventUnion, Tag("meta_event")],
    Discriminator("post_type"),
]
