from __future__ import annotations

from abc import ABC
from functools import cached_property
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag
from pydantic_ai.messages import UserContent

from octomate.schemas.segments import AtSegment, MessageSegment, TextSegment
from octomate.schemas.session import Anonymous, SessionKey, UserProfile


class Event(BaseModel, ABC):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    time: int = 0
    self_id: str = "0"
    tentacle_id: str = ""


class MessageEvent(Event, ABC):
    post_type: Literal["message"] = "message"
    message_type: Literal["private", "group"]

    message_id: str
    user_id: str
    font: int = 0

    sender: UserProfile = Field(default=UserProfile())
    message: list[MessageSegment] = Field(default_factory=list)
    raw_message: str = ""

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(tentacle_id=self.tentacle_id, user_id=self.user_id)

    @property
    def display_name(self) -> str:
        return self.sender.name or self.sender.nickname or "anonymous"

    def __str__(self) -> str:
        body = "\n".join(f"  {seg}" for seg in self.message)
        return f"{self.display_name} ({self.user_id}) #msg:{self.message_id}:\n{body}"

    def to_content_parts(self) -> list[UserContent]:
        header = f"{self.display_name} ({self.user_id}) #msg:{self.message_id}:"
        return [header] + [seg.to_content() for seg in self.message]


class PrivateMessageEvent(MessageEvent):
    message_type: Literal["private"] = "private"
    sub_type: str = "friend"


class GroupMessageEvent(MessageEvent):
    message_type: Literal["group"] = "group"

    group_id: str
    anonymous: Anonymous | None = None

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(
            tentacle_id=self.tentacle_id,
            user_id=self.user_id,
            group_id=self.group_id,
        )

    def is_at(self, user_id: str | None = None) -> bool:
        return user_id is not None and any(
            isinstance(seg, AtSegment) and seg.data.user_id == user_id
            for seg in self.message
        )

    def filter_text(self) -> list[str]:
        return [
            seg.data["text"] for seg in self.message if isinstance(seg, TextSegment)
        ]

    @property
    def display_name(self) -> str:
        return self.sender.name or self.sender.nickname or "anonymous"


MessageEventUnion = Annotated[
    GroupMessageEvent | PrivateMessageEvent,
    Discriminator("message_type"),
]


class NoticeEvent(Event):
    post_type: Literal["notice"] = "notice"


class GroupNoticeEvent(NoticeEvent):
    group_id: str
    user_id: str

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
        return SessionKey(tentacle_id=self.tentacle_id, user_id=self.user_id)


class FriendRecallNotice(NoticeEvent):
    notice_type: Literal["friend_recall"] = "friend_recall"
    user_id: str
    message_id: str

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(tentacle_id=self.tentacle_id, user_id=self.user_id)


class GroupPokeNotice(NoticeEvent):
    notice_type: Literal["notify"] = "notify"
    sub_type: Literal["poke"] = "poke"
    group_id: str | None = None
    user_id: str
    target_id: str

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
    group_id: str
    user_id: str
    target_id: str

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(
            tentacle_id=self.tentacle_id, user_id=self.user_id, group_id=self.group_id
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
            tentacle_id=self.tentacle_id, user_id=self.user_id, group_id=self.group_id
        )


class MsgEmojiLikeNotice(GroupNoticeEvent):
    notice_type: Literal["group_msg_emoji_like"] = "group_msg_emoji_like"
    message_id: str
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


class RequestEvent(Event):
    post_type: Literal["request"] = "request"


class FriendRequest(RequestEvent):
    request_type: Literal["friend"] = "friend"
    user_id: str
    comment: str = ""
    flag: str = ""

    @cached_property
    def session_key(self) -> SessionKey:
        return SessionKey(tentacle_id=self.tentacle_id, user_id=self.user_id)


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
            tentacle_id=self.tentacle_id, user_id=self.user_id, group_id=self.group_id
        )


RequestEventUnion = Annotated[
    FriendRequest | GroupRequest,
    Discriminator("request_type"),
]


class MetaEvent(Event):
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


EventUnion = Annotated[
    Annotated[MessageEventUnion, Tag("message")]
    | Annotated[NoticeEventUnion, Tag("notice")]
    | Annotated[RequestEventUnion, Tag("request")]
    | Annotated[MetaEventUnion, Tag("meta_event")],
    Discriminator("post_type"),
]
