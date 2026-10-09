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

from arcanus.dataclass import rebuild_dataclass

from octomate.schemas import (
    actions,
    conversation,
    deferred,
    events,
    messages,
    segments,
    thread,
    todos,
    triage,
)
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import MessageSegment

# The thread and message schemas name each other, so neither module can import
# the other at its top, and they are completed here once both exist. Arcanus
# resolves a transmuter's relation hints in its own module and re-reads an
# unresolved one on every access, so each module is given the other's names.
messages.__dict__.update(
    ThreadMessage=thread.ThreadMessage, ThreadCommand=thread.ThreadCommand
)
thread.__dict__.update(
    ModelRequest=messages.ModelRequest, ModelResponse=messages.ModelResponse
)
thread.ThreadMessage.model_rebuild()
thread.ThreadCommand.model_rebuild()
messages.ModelMessage.model_rebuild()
rebuild_dataclass(messages.ModelRequest)
rebuild_dataclass(messages.ModelResponse)

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
