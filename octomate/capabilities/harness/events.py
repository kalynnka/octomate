"""Typed event stream the agent run produces and channels consume.

One stream, with these event families:

- **Pydantic AI passthrough** (`AgentStreamEvent`) — thinking + tool-call events.
- **Output values** — text replies stream as Pydantic AI's native `TextPart`
  events, every validated output type is emitted as Pydantic AI's own
  `FinalResult[OutputT]`, and outputs that validate to `list[MessageSegment]`
  additionally stream one `ResultSegmentEvent` per completed segment as partial
  validation reveals it. Rendering (typing text, sending segments, or handling
  any other structured value) is a *consumer* concern; the event just carries the
  typed value.
- **Display events** (`DisplayEvent`) — fire-and-forget, e.g. the granular todo
  events (`TodoCreatedEvent`/`TodoUpdatedEvent`/`TodoStatusChangedEvent`/
  `TodoCompletedEvent`/`TodoDeletedEvent`), each carrying the affected `Todo`.
- **Action batch** (`ActionBatchEvent`) — a persisted batch of deferred actions
  (questions + approvals) presented as one unit; the run suspends until the user
  replies. `batch_id` correlates the reply through the deferred-action machinery.

Display and action events are emitted by capabilities (a capability bundles a tool
+ instructions + `wrap_run_event_stream`); the output events are emitted by
`Agent.stream_events` (see octomate/capabilities/harness/agent.py).

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

SubagentActivityKind = Literal["scheme", "whisper"]
SubagentActivityStatus = Literal["completed", "failed", "timed_out", "cancelled"]


@dataclass(frozen=True)
class SubagentActivity:
    """One commissioned child run rendered on its own channel timeline."""

    invocation_id: str
    kind: SubagentActivityKind
    name: str


@dataclass
class ResultSegmentEvent:
    """One completed message segment from a `list[MessageSegment]` output.

    This is a convenience streaming event for channel renderers. The full typed
    output, segment list or otherwise, still arrives as `FinalResult[OutputT]`.
    """

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


class MessageSentEvent(DisplayEvent):
    """The send capability emitted `segments` to be delivered to the run's
    conversation mid-run, without ending the turn. Emit-only: the tool does not
    touch any channel — the consumer rendering this run's stream (the channel
    timeline, or the web event stream) renders these segments into the current
    conversation, the same way it renders a streamed reply."""

    event_kind: Literal["message_sent"] = "message_sent"
    segments: list[MessageSegment]


class OAuthAuthorizationEvent(DisplayEvent):
    """An integration is waiting for this user to authorize their own account.

    Carries the authorization itself rather than a rendered message, so each channel
    presents it as well as it can — a link and code in a plain timeline, a card whose
    button continues the flow where the platform has one. `connector_id` is what a
    consumer completes the connection against once the user acts.
    """

    event_kind: Literal["oauth_authorization"] = "oauth_authorization"
    connector_id: str
    label: str
    verification_uri: str
    user_code: str


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
    | ResultSegmentEvent
    | FinalResult[OutputT]
    | TodoEvent
    | MessageSentEvent
    | OAuthAuthorizationEvent
    | ActionBatchEvent,
    type_params=(OutputT,),
)
