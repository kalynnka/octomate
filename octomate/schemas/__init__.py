"""
Shared schema package.

Submodules
----------
- ``session``: SessionKey, UserProfile.
- ``segments``: Message segment data types and models.
- ``actions``: Outbound action models.
- ``events``: Platform-agnostic event models.
"""

from octomate.schemas import actions, events, segments, session
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import AgentSegment, MessageSegment

__all__ = [
    "AgentSegment",
    "MessageEvent",
    "MessageSegment",
    "actions",
    "events",
    "segments",
    "session",
]
