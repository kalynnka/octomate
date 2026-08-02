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

from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import DeferredApproval, DeferredQuestion
from octomate.schemas.segments import MessageSegment
from octomate.schemas.todos import Todo

OutputT = TypeVar("OutputT")

SubagentActivityKind = Literal["commission", "whisper"]
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
    """The gate's `send` emitted `segments` to be delivered mid-run, without ending
    the turn. Emit-only: the tool touches no channel. Where it goes is settled by
    the time this is emitted — `destination` is None for the run's own conversation,
    and otherwise a surface the gate resolved from the identity registry."""

    event_kind: Literal["message_sent"] = "message_sent"
    segments: list[MessageSegment]
    destination: ChannelAddress | None = Field(
        default=None,
        description="None for this conversation. Otherwise the address the gate "
        "already resolved from the handle the model named, so the consumer delivers "
        "without re-deciding anything and a refused destination never reaches one.",
    )


class OAuthAuthorizationEvent(DisplayEvent):
    """An integration is waiting for this user to authorize their own account.

    Carries the authorization itself rather than a rendered message, so each channel
    presents it as well as it can — a link in a plain timeline, a card whose button
    opens the page where the platform has one. `connector_id` is the connection this
    authorization belongs to.

    Emitted as it stands by an authorization-code flow, where opening the link and
    approving is the whole errand: nothing to copy, and nothing to come back for
    because the provider's callback finishes the connection.
    """

    event_kind: Literal["oauth_authorization", "oauth_device_authorization"] = (
        "oauth_authorization"
    )
    connector_id: str
    label: str
    authorization_uri: str = Field(
        description="The page the user opens. A device flow's verification page, or "
        "the UUID-only link an authorization-code flow staged its provider request "
        "behind."
    )


class OAuthDeviceAuthorizationEvent(OAuthAuthorizationEvent):
    """A device flow, which asks the user for one thing more.

    Finishing is a second errand they have to come back for, which is why the code
    travels with the link and why a presenter asks them to return. A presenter that
    does not distinguish the two renders this as its base and simply leaves the code
    out — wrong, but not misleading.
    """

    # The declared type has to stay the base's, or the override is a narrowing of a
    # mutable field; only the default changes, which is what names this on the wire.
    event_kind: Literal["oauth_authorization", "oauth_device_authorization"] = (
        "oauth_device_authorization"
    )
    user_code: str = Field(description="The one-time code the user types on the page.")


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
