"""Typed event stream the agent run produces and channels consume.

One stream, with these event families:

- **Pydantic AI passthrough** (`AgentStreamEvent`) — thinking + tool-call events.
- **Output events** — the agent's reply, in one of two modes mirroring Pydantic AI's
  own streaming: `ResultTextDeltaEvent` (an additive text chunk — PAI's `content_delta`
  typewriter) while a *text* reply streams, or `ResultSegmentEvent` (one completed
  `MessageSegment`) as a *`list[MessageSegment]`* reply is partially validated. Both
  precede Pydantic AI's own `FinalResult[OutputT]`, emitted as the final value — no
  wrapper of our own. Rendering (typing the text, sending a segment) is a *consumer*
  concern; the event just carries the typed value.
- **Display events** (`DisplayEvent`) — fire-and-forget, e.g. the granular todo
  events (`TodoCreatedEvent`/`TodoUpdatedEvent`/`TodoStatusChangedEvent`/
  `TodoCompletedEvent`/`TodoDeletedEvent`), each carrying the affected `Todo`.
- **Action batch** (`ActionBatchEvent`) — a persisted batch of deferred actions
  (questions + approvals) presented as one unit; the run suspends until the user
  replies. `batch_id` correlates the reply through the deferred-action machinery.

Display and action events are emitted by capabilities (a capability bundles a tool
+ instructions + `wrap_run_event_stream`); the output events are emitted by
`Agent.stream_events` (see octomate/capabilities/agent.py).

Serialization (a wire format for dev_ui) is intentionally not modelled here yet:
the output events are generic, so a single discriminated `TypeAdapter` no longer
fits — that belongs with the consumer/transport layer when dev_ui adopts this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias, TypeVar

from pydantic import BaseModel, Field
from pydantic_ai import AgentStreamEvent
from pydantic_ai.result import FinalResult
from typing_extensions import TypeAliasType

from octomate.schemas.deferred import DeferredApproval, DeferredQuestion
from octomate.schemas.segments import MessageSegment
from octomate.schemas.todos import Todo

OutputT = TypeVar("OutputT")


@dataclass
class ResultTextDeltaEvent:
    """An additive text chunk of a streamed text reply — Pydantic AI's typewriter
    (the model's `content_delta`), not a re-derived diff."""

    delta: str
    event_kind: Literal["result_text_delta"] = "result_text_delta"


@dataclass
class ResultSegmentEvent:
    """One completed message segment of a `list[MessageSegment]` reply, emitted as
    partial validation reveals it."""

    segment: MessageSegment
    event_kind: Literal["result_segment"] = "result_segment"


class DisplayEvent(BaseModel):
    """Fire-and-forget outbound event; the run continues after it is emitted."""


class TodoCreatedEvent(DisplayEvent):
    event_kind: Literal["todo_created"] = "todo_created"
    todo: Todo


class TodoUpdatedEvent(DisplayEvent):
    event_kind: Literal["todo_updated"] = "todo_updated"
    todo: Todo
    previous: Todo | None = None


class TodoStatusChangedEvent(DisplayEvent):
    event_kind: Literal["todo_status_changed"] = "todo_status_changed"
    todo: Todo
    previous: Todo | None = None


class TodoCompletedEvent(DisplayEvent):
    event_kind: Literal["todo_completed"] = "todo_completed"
    todo: Todo


class TodoDeletedEvent(DisplayEvent):
    event_kind: Literal["todo_deleted"] = "todo_deleted"
    todo: Todo


# The granular todo events a capability emits as the agent mutates its todo list.
TodoEvent: TypeAlias = (
    TodoCreatedEvent
    | TodoUpdatedEvent
    | TodoStatusChangedEvent
    | TodoCompletedEvent
    | TodoDeletedEvent
)


class ActionBatchEvent(BaseModel):
    """A persisted batch of deferred actions presented as one unit; the run
    suspends until the user replies.

    Carries the batch's `questions` and `approvals` (the persisted actions) so the
    consumer renders + marks them together, and `batch_id` to correlate the reply
    back to the right run.
    """

    event_kind: Literal["action_batch"] = "action_batch"
    batch_id: str
    questions: list[DeferredQuestion] = Field(default_factory=list)
    approvals: list[DeferredApproval] = Field(default_factory=list)


# The stream a consumer matches on, generic over the run's output type.
# (TypeAliasType backports PEP 695's generic alias to the project's 3.11 floor.)
StreamEvents = TypeAliasType(
    "StreamEvents",
    AgentStreamEvent
    | ResultTextDeltaEvent
    | ResultSegmentEvent
    | FinalResult[OutputT]
    | TodoEvent
    | ActionBatchEvent,
    type_params=(OutputT,),
)
