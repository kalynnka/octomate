"""
Shared schema package.

Submodules
----------
- ``session``: SessionKey, UserProfile.
- ``segments``: Message segment data types and models.
- ``actions``: Outbound action models.
- ``events``: Platform-agnostic event models.
- ``plan``: Lightweight internal plan/step models for multi-step decomposition.
"""

from octomate.schemas import actions, events, plan, segments, session
from octomate.schemas.events import MessageEvent
from octomate.schemas.plan import Plan, PlanStep
from octomate.schemas.segments import AgentSegment, MessageSegment

__all__ = [
    "AgentSegment",
    "MessageEvent",
    "MessageSegment",
    "Plan",
    "PlanStep",
    "actions",
    "events",
    "plan",
    "segments",
    "session",
]
