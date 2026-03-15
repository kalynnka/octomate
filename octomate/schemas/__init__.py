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
    action_adapter,
)
from octomate.schemas.events import (
    EventUnion,
    MessageEventUnion,
    MetaEventUnion,
    NoticeEventUnion,
    NotifyEventUnion,
    RequestEventUnion,
)
from octomate.schemas.segments import MessageSegment

__all__ = [
    "ActionUnion",
    "action_adapter",
    "EventUnion",
    "MessageEventUnion",
    "MessageSegment",
    "MetaEventUnion",
    "NoticeEventUnion",
    "NotifyEventUnion",
    "RequestEventUnion",
    "actions",
    "adaptors",
    "events",
    "segments",
    "session",
]
