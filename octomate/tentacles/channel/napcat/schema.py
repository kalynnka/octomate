"""Napcat (OneBot 11) specific schema types.

Contains all OneBot 11-shaped types removed from core schemas, plus
Napcat-specific overrides (NapcatAtSegment, etc.) and the inbound
frame adapter used by NapcatTentacle.
"""

from __future__ import annotations

from abc import ABC
from functools import cached_property
from typing import Annotated, Any, Literal, NotRequired, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, TypeAdapter
from typing_extensions import TypedDict

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import (
    AgentSegment,
    AtData,
    AtSegment,
    ImageSegment,
    MessageSegment,
    ReplySegment,
    Segment,
    TextSegment,
)
from octomate.schemas.session import SessionKey, UserProfile


# OneBot 11 — action types (moved from schemas/actions.py)
class ActionResponse(BaseModel):
    """Response received after sending an action via WebSocket."""

    status: str = ""
    retcode: int = 0
    data: dict[str, Any] | None = None
    echo: str | None = None
    message: str | None = None
    wording: str | None = None


class SendGroupMsgParams(BaseModel):
    group_id: int | str
    message: list[AgentSegment]
    reply: int | str | None = None


class SendGroupMsgAction(BaseModel):
    action: Literal["send_group_msg"] = "send_group_msg"
    tentacle_id: str
    params: SendGroupMsgParams


class SendPrivateMsgParams(BaseModel):
    user_id: int | str
    message: list[AgentSegment]
    reply: int | str | None = None


class SendPrivateMsgAction(BaseModel):
    action: Literal["send_private_msg"] = "send_private_msg"
    tentacle_id: str
    params: SendPrivateMsgParams


class CallApiAction(BaseModel):
    action: str
    tentacle_id: str
    params: dict[str, Any] = Field(default_factory=dict)


def _action_discriminator(raw: Any) -> str:
    if isinstance(raw, dict):
        action = raw.get("action", "")
    else:
        action = getattr(raw, "action", "")
    if action in ("send_group_msg", "send_private_msg"):
        return action
    return "__default__"


ActionUnion = Annotated[
    Union[
        Annotated[SendGroupMsgAction, Tag("send_group_msg")],
        Annotated[SendPrivateMsgAction, Tag("send_private_msg")],
        Annotated[CallApiAction, Tag("__default__")],
    ],
    Discriminator(_action_discriminator),
]

action_adapter: TypeAdapter[ActionUnion] = TypeAdapter(ActionUnion)


# OneBot 11 — segment types (moved from schemas/segments.py)
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


class FaceSegment(Segment):
    type: Literal["face"] = "face"
    data: FaceData

    def __str__(self) -> str:
        return f"[face: {self.data['id']}]"


class RecordSegment(Segment):
    type: Literal["record"] = "record"
    data: RecordData

    def __str__(self) -> str:
        return "[audio]"


class VideoSegment(Segment):
    type: Literal["video"] = "video"
    data: VideoData

    def __str__(self) -> str:
        return "[video]"


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

    def __str__(self) -> str:
        return f"[share: {self.data['title']} {self.data['url']}]"


class ContactSegment(Segment):
    type: Literal["contact"] = "contact"
    data: ContactData

    def __str__(self) -> str:
        return f"[contact: {self.data['type']}:{self.data['id']}]"


class LocationSegment(Segment):
    type: Literal["location"] = "location"
    data: LocationData

    def __str__(self) -> str:
        label = self.data.get("title") or f"({self.data['lat']},{self.data['lon']})"
        return f"[location: {label}]"


class MusicSegment(Segment):
    type: Literal["music"] = "music"
    data: MusicData

    def __str__(self) -> str:
        label = self.data.get("title") or self.data["type"]
        return f"[music: {label}]"


class ForwardSegment(Segment):
    type: Literal["forward"] = "forward"
    data: ForwardData

    def __str__(self) -> str:
        return f"[forward: {self.data['id']}]"


class NodeSegment(Segment):
    type: Literal["node"] = "node"
    data: NodeData


class XmlSegment(Segment):
    type: Literal["xml"] = "xml"
    data: XmlData


class JsonSegment(Segment):
    type: Literal["json"] = "json"
    data: JsonData


# OneBot 11 event types (moved from schemas/events.py)


class Anonymous(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    id: str = "0"
    name: str = ""


class OneBotEvent(BaseModel, ABC):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    time: int = 0
    self_id: str = "0"
    tentacle_id: str = ""


class OneBotMessageEvent(OneBotEvent, ABC):
    post_type: Literal["message"] = "message"
    message_type: Literal["private", "group"]

    message_id: str
    user_id: str
    font: int = 0

    sender: UserProfile = Field(default=UserProfile())
    message: list[Any] = Field(default_factory=list)
    raw_message: str = ""

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(
            tentacle_id=self.tentacle_id, user_id=self.user_id, chat_id=self.user_id
        )

    @property
    def display_name(self) -> str:
        return self.sender.name or self.sender.nickname or "anonymous"

    def __str__(self) -> str:
        body = "\n".join(f"  {seg}" for seg in self.message)
        return f"{self.display_name} ({self.user_id}) #msg:{self.message_id}:\n{body}"


class PrivateMessageEvent(OneBotMessageEvent):
    message_type: Literal["private"] = "private"
    sub_type: str = "friend"


class GroupMessageEvent(OneBotMessageEvent):
    message_type: Literal["group"] = "group"

    group_id: str
    anonymous: Anonymous | None = None

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(
            tentacle_id=self.tentacle_id,
            user_id=self.user_id,
            group_id=self.group_id,
            chat_id=self.group_id,
        )

    def is_at(self, user_id: str | None = None) -> bool:
        return user_id is not None and any(
            isinstance(seg, AtSegment) and seg.data.user_id == user_id
            for seg in self.message
        )


MessageEventUnion = Annotated[
    GroupMessageEvent | PrivateMessageEvent,
    Discriminator("message_type"),
]


class NoticeEvent(OneBotEvent):
    post_type: Literal["notice"] = "notice"


class GroupNoticeEvent(NoticeEvent):
    group_id: str
    user_id: str

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(
            tentacle_id=self.tentacle_id,
            user_id=self.user_id,
            group_id=self.group_id,
            chat_id=self.group_id,
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
    operator_id: str


class GroupIncreaseNotice(GroupNoticeEvent):
    notice_type: Literal["group_increase"] = "group_increase"
    sub_type: Literal["approve", "invite"]
    operator_id: str


class GroupBanNotice(GroupNoticeEvent):
    notice_type: Literal["group_ban"] = "group_ban"
    sub_type: Literal["ban", "lift_ban"]
    operator_id: str
    duration: int = 0


class GroupRecallNotice(GroupNoticeEvent):
    notice_type: Literal["group_recall"] = "group_recall"
    operator_id: str
    message_id: str


class GroupCardNotice(GroupNoticeEvent):
    notice_type: Literal["group_card"] = "group_card"
    card_new: str = ""
    card_old: str = ""


class GroupEssenceNotice(GroupNoticeEvent):
    notice_type: Literal["group_essence"] = "group_essence"
    sub_type: Literal["add", "delete"]
    operator_id: str
    message_id: str


class FriendAddNotice(NoticeEvent):
    notice_type: Literal["friend_add"] = "friend_add"
    user_id: str

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(
            tentacle_id=self.tentacle_id, user_id=self.user_id, chat_id=self.user_id
        )


class FriendRecallNotice(NoticeEvent):
    notice_type: Literal["friend_recall"] = "friend_recall"
    user_id: str
    message_id: str

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(
            tentacle_id=self.tentacle_id, user_id=self.user_id, chat_id=self.user_id
        )


class GroupPokeNotice(NoticeEvent):
    notice_type: Literal["notify"] = "notify"
    sub_type: Literal["poke"] = "poke"
    group_id: str | None = None
    user_id: str
    target_id: str

    @cached_property
    def session_key(self) -> SessionKey:
        if self.group_id:
            return SessionKey(
                tentacle_id=self.tentacle_id,
                user_id=self.user_id,
                group_id=self.group_id,
                chat_id=self.group_id,
            )
        return SessionKey(
            tentacle_id=self.tentacle_id, user_id=self.user_id, chat_id=self.user_id
        )


class GroupLuckyKingNotice(NoticeEvent):
    notice_type: Literal["notify"] = "notify"
    sub_type: Literal["lucky_king"] = "lucky_king"
    group_id: str
    user_id: str
    target_id: str

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(
            tentacle_id=self.tentacle_id,
            user_id=self.user_id,
            group_id=self.group_id,
            chat_id=self.group_id,
        )


class GroupHonorNotice(NoticeEvent):
    notice_type: Literal["notify"] = "notify"
    sub_type: Literal["honor"] = "honor"
    group_id: str
    user_id: str
    honor_type: str

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(
            tentacle_id=self.tentacle_id,
            user_id=self.user_id,
            group_id=self.group_id,
            chat_id=self.group_id,
        )


class MsgEmojiLikeNotice(GroupNoticeEvent):
    notice_type: Literal["group_msg_emoji_like"] = "group_msg_emoji_like"
    message_id: str
    likes: list[dict[str, Any]] = Field(default_factory=list)


class RequestEvent(OneBotEvent):
    post_type: Literal["request"] = "request"


class FriendRequest(RequestEvent):
    request_type: Literal["friend"] = "friend"
    user_id: str
    comment: str = ""
    flag: str = ""

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(
            tentacle_id=self.tentacle_id, user_id=self.user_id, chat_id=self.user_id
        )


class GroupRequest(RequestEvent):
    request_type: Literal["group"] = "group"
    sub_type: str = "add"
    group_id: str
    user_id: str
    comment: str = ""
    flag: str = ""

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(
            tentacle_id=self.tentacle_id,
            user_id=self.user_id,
            group_id=self.group_id,
            chat_id=self.group_id,
        )


class MetaEvent(OneBotEvent):
    post_type: Literal["meta_event"] = "meta_event"


class LifecycleEvent(MetaEvent):
    meta_event_type: Literal["lifecycle"] = "lifecycle"
    sub_type: str = ""


class HeartbeatEvent(MetaEvent):
    meta_event_type: Literal["heartbeat"] = "heartbeat"
    status: dict[str, Any] = Field(default_factory=dict)
    interval: int = 0


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

RequestEventUnion = Annotated[
    FriendRequest | GroupRequest,
    Discriminator("request_type"),
]

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


# Napcat-specific overrides
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


def _inbound_discriminator(raw: Any) -> str:
    if isinstance(raw, dict) and "post_type" in raw:
        return "event"
    if isinstance(raw, OneBotEvent):
        return "event"
    return "response"


InboundFrame = Annotated[
    Annotated[NapcatEventUnion, Tag("event")]
    | Annotated[ActionResponse, Tag("response")],
    Discriminator(_inbound_discriminator),
]

inbound_adapter: TypeAdapter[InboundFrame] = TypeAdapter(InboundFrame)


COMPATIBLE = (TextSegment, AtSegment, ImageSegment, ReplySegment)


# Conversion: Napcat event → platform-agnostic MessageEvent
def to_message_event(
    ev: NapcatGroupMessageEvent | NapcatPrivateMessageEvent,
) -> MessageEvent:
    segments: list[MessageSegment] = [
        s for s in ev.message if isinstance(s, COMPATIBLE)
    ]

    reply_id = ""
    for s in segments:
        if isinstance(s, ReplySegment):
            reply_id = s.data["id"]
            break

    if isinstance(ev, NapcatGroupMessageEvent):
        chat_type: Literal["private", "group"] = "group"
        chat_id = ev.group_id
    else:
        chat_type = "private"
        chat_id = ev.user_id

    return MessageEvent(
        message_id=ev.message_id,
        reply_id=reply_id,
        timestamp=float(ev.time),
        user_id=ev.user_id,
        chat_id=chat_id,
        chat_type=chat_type,
        self_id=ev.self_id,
        sender=ev.sender,
        segments=segments,
        raw=ev.raw_message,
    )
