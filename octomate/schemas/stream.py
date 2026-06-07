"""Typed outbound event stream the agent run produces and channels consume.

The agent run is a pure *producer* of `OctoStreamEvent`s; channels *subscribe* and
render them via feelers. Events split into two lifecycle groups:

- `DisplayEvent` — fire-and-forget, mid-run; the run continues after emitting.
- `ActionRequestEvent` — needs a user reply; the run suspends. `action_id`/
  `batch_id` correlate the reply back through the deferred-action machinery.

An outbound message streams as a lifecycle trio (`SayStartEvent` →
`SayChunkEvent`(s) → `SayEndEvent`) sharing a `message_id`, so a
channel can create-then-edit a single platform message as the agent's structured
output fills in. This mirrors Pydantic AI's `stream_output()` (partial validated
snapshots), which the low-level event API cannot surface on its own.

`OctoStreamEvent` is the single discriminated union the rest of the system speaks:
Pydantic AI's native events pass through unchanged, plus the octomate-defined
events here. Content reuses the bidirectional `MessageSegment` vocabulary and
action variants embed the existing `QuestionRequest`/`ApprovalRequest` payloads —
no shape is redeclared.

Outbound targeting (the agent choosing which channel to send to) is a future,
router-level concern: a `target` field will be added to the outbound events here
once the channel dispatcher exists. It is intentionally omitted now — see
docs/plans/agent-event-stream.md (Outbound targeting).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Discriminator, TypeAdapter
from pydantic_ai import AgentStreamEvent

from octomate.schemas.deferred import ApprovalRequest, QuestionRequest
from octomate.schemas.segments import MessageSegment


class TodoItem(BaseModel):
    content: str
    status: Literal["pending", "in_progress", "completed"] = "pending"


class DisplayEvent(BaseModel):
    """Fire-and-forget outbound event; the run continues after it is emitted."""


class SayEvent(DisplayEvent):
    """Base for the streaming lifecycle of one outbound message.

    A message streams as start → chunk(s) → end, all sharing `message_id`, so a
    channel can create-then-edit a single platform message as content fills in.
    """

    message_id: str


class SayStartEvent(SayEvent):
    event_kind: Literal["say_start"] = "say_start"


class SayChunkEvent(SayEvent):
    event_kind: Literal["say_chunk"] = "say_chunk"
    segments: list[MessageSegment]
    """Cumulative partial snapshot of the message so far."""


class SayEndEvent(SayEvent):
    event_kind: Literal["say_end"] = "say_end"
    segments: list[MessageSegment]
    """Authoritative final content of the message."""


class TodoListEvent(DisplayEvent):
    event_kind: Literal["todo_list"] = "todo_list"
    items: list[TodoItem]


class ActionRequestEvent(BaseModel):
    """Outbound event that needs a user reply; the run suspends until resolved.

    `action_id`/`batch_id` reference the persisted `DeferredAction`/
    `DeferredActionBatch` so the eventual reply resumes the right run.
    """

    action_id: str
    batch_id: str


class AskQuestionEvent(ActionRequestEvent):
    event_kind: Literal["ask_question"] = "ask_question"
    question: QuestionRequest


class ApprovalRequestEvent(ActionRequestEvent):
    event_kind: Literal["approval_request"] = "approval_request"
    approval: ApprovalRequest


# Single union the producer yields and consumers match on: Pydantic AI passthrough
# events plus the octomate-defined events above, discriminated by `event_kind`.
OctoStreamEvent = Annotated[
    AgentStreamEvent
    | SayStartEvent
    | SayChunkEvent
    | SayEndEvent
    | TodoListEvent
    | AskQuestionEvent
    | ApprovalRequestEvent,
    Discriminator("event_kind"),
]
OctoStreamEventAdapter: TypeAdapter[OctoStreamEvent] = TypeAdapter(OctoStreamEvent)
OctoStreamEventListAdapter: TypeAdapter[list[OctoStreamEvent]] = TypeAdapter(
    list[OctoStreamEvent]
)
