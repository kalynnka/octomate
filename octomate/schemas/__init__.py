"""
Shared schema package.

Submodules
----------
- ``session``: SessionKey, Sender, Anonymous.
- ``segments``: Message segment data types and models.
- ``actions``: Outbound action models, unions, and TypeAdapters.
- ``events``: OneBot 11 event models and unions.
"""

from octomate.schemas import actions, events, segments, session
from octomate.schemas.actions import (
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
    "events",
    "segments",
    "session",
]
