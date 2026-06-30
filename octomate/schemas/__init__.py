"""
Shared schema package.

Submodules
----------
- ``thread``: ThreadKey, Thread, ThreadMessage, Handoff,
  MessageBinding.
- ``conversation``: ChannelAddress, ConversationKey, Conversation, UserProfile.
- ``segments``: Message segment data types and models.
- ``actions``: Outbound action models.
- ``events``: Platform-agnostic event models.
- ``triage``: Graph routing decisions.
- ``deferred``: Deferred human-in-the-loop action records.
- ``todos``: Conversation-scoped todo records.
"""

from octomate.schemas import (
    actions,
    conversation,
    deferred,
    events,
    segments,
    thread,
    todos,
    triage,
)
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import MessageSegment

__all__ = [
    "MessageEvent",
    "MessageSegment",
    "actions",
    "conversation",
    "deferred",
    "events",
    "segments",
    "thread",
    "todos",
    "triage",
]
