"""Pre-built Pydantic TypeAdapters for common unions."""

from __future__ import annotations

from typing import Annotated, Any, Union

from pydantic import Discriminator, Tag, TypeAdapter

from octomate.schemas.actions import (
    ActionResponse,
    CallApiAction,
    SendGroupMsgAction,
    SendPrivateMsgAction,
)
from octomate.schemas.events import OneBotEvent, OneBotEventUnion


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


def action_discriminator(raw: Any) -> str:
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
    Discriminator(action_discriminator),
]

action_adapter: TypeAdapter[ActionUnion] = TypeAdapter(ActionUnion)
