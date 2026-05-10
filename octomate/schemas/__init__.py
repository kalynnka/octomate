"""
Shared schema package.

Submodules
----------
- ``conversation``: ConversationKey, Conversation, UserProfile.
- ``segments``: Message segment data types and models.
- ``actions``: Outbound action models.
- ``events``: Platform-agnostic event models.
"""

from octomate.schemas import actions, conversation, events, segments
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import MessageSegment

__all__ = [
    "MessageEvent",
    "MessageSegment",
    "actions",
    "conversation",
    "events",
    "segments",
]
