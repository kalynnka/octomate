"""
Shared schema package.

Submodules
----------
- ``session``: SessionKey, Sender, Anonymous.
- ``segments``: Message segment data types and models.
- ``actions``: Outbound action models and ActionResponse.
- ``events``: OneBot 11 event models and unions.
- ``adaptors``: Pre-built TypeAdapters for common unions.
"""

from octomate.schemas import actions, adaptors, events, segments, session
from octomate.schemas.adaptors import (
    ActionUnion,
    InboundFrame,
    action_adapter,
    inbound_adapter,
)
from octomate.schemas.events import (
    MessageEventUnion,
    MetaEventUnion,
    NoticeEventUnion,
    NotifyEventUnion,
    OneBotEventUnion,
    RequestEventUnion,
)
from octomate.schemas.segments import MessageSegment

__all__ = [
    "ActionUnion",
    "InboundFrame",
    "action_adapter",
    "MessageEventUnion",
    "MessageSegment",
    "MetaEventUnion",
    "NoticeEventUnion",
    "NotifyEventUnion",
    "OneBotEventUnion",
    "RequestEventUnion",
    "actions",
    "adaptors",
    "events",
    "inbound_adapter",
    "segments",
    "session",
]
