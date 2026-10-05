"""Typed event stream the agent run produces and channels consume.

One stream, with these event families:

- **Pydantic AI passthrough** (`AgentStreamEvent`) — thinking + tool-call events.
- **Output values** — text replies stream as Pydantic AI's native `TextPart`
  events, every validated output type is emitted as Pydantic AI's own
  `FinalResult[OutputT]`, and outputs that validate to `list[MessageSegment]`
  additionally stream one `ResultSegmentEvent` per completed segment as partial
  validation reveals it — except a trailing text segment, whose growth streams
  as `ResultTextDeltaEvent`s instead. Rendering (typing text, sending segments,
  or handling any other structured value) is a *consumer* concern; the event
  just carries the typed value.
- **Display events** (`DisplayEvent`) — fire-and-forget, e.g. the granular todo
  events (`TodoCreatedEvent`/`TodoUpdatedEvent`/`TodoStatusChangedEvent`/
  `TodoCompletedEvent`/`TodoDeletedEvent`), each carrying the affected `Todo`.
- **Action batch** (`ActionBatchEvent`) — a persisted batch of deferred actions
  (questions + approvals) presented as one unit; the run suspends until the user
  replies. `batch_id` correlates the reply through the deferred-action machinery.
- **Subagent lifecycle** (`SubagentStartedEvent`/`SubagentSettledEvent`) — an
  accomplice's run opening and finishing around its `commission` or `whisper` call.
- **Graph events** (`RunStartedEvent`) — Pydantic AI `CustomEvent`s the reflex
  graph puts on the stream it drives, about the run rather than inside it.

Display, action and subagent events are emitted by capabilities (a capability
bundles a tool + instructions + `wrap_run_event_stream`) or the suspender; the
output events are emitted by `Agent.stream_events` (see
octomate/capabilities/harness/agent.py).

The reflex graph also reports outside any run (`GatewayEvent`, `RunErrorEvent`),
handing each to a channel's `Feelers.present`. Channels present every event and
build none; the one consumer-made form is `RunResultEvent`, the wire shape of a
run result.

The run-stream union itself stays generic (`FinalResult[OutputT]`), so it has
no single serialized form — but the wire family is concrete, so `WireEvent` and
its `wire_event_adapter` live here too: the run stream as a wire consumer sees
it, with the generic/unserializable members replaced by their wire forms.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_ai import AgentStreamEvent, CustomEvent
from pydantic_ai.result import FinalResult
from pydantic_ai.usage import RunUsage

from octomate.schemas.auth import LinkProfileAuthorization
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import (
    DeferredActionBatch,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.schemas.segments import MessageSegment
from octomate.schemas.todos import Todo
from octomate.schemas.triage import GatewayAction

SubagentActivityKind = Literal["commission", "whisper"]
SubagentActivityStatus = Literal["completed", "failed", "timed_out", "cancelled"]


class SubagentStartedEvent(BaseModel):
    """An accomplice's run opened, named by the call that started it; a channel
    draws it on a timeline of its own."""

    event_kind: Literal["subagent_started"] = "subagent_started"
    invocation_id: str
    kind: SubagentActivityKind
    name: str


class SubagentSettledEvent(BaseModel):
    """The child run finished; `response` is its accumulated reply text."""

    event_kind: Literal["subagent_settled"] = "subagent_settled"
    invocation_id: str
    status: SubagentActivityStatus
    detail: str | None = None
    response: str = ""


@dataclass(kw_only=True)
class RunStartedEvent(CustomEvent):
    """A run's first event, from the graph: the thread it reports into — another
    than the one asked about when a move carried the turn there."""

    address: ChannelAddress


class RunResultEvent(BaseModel):
    """The serializable projection of `AgentRunResultEvent`, whose payload
    drags the run's private graph state and cannot go on a wire. Carries the
    output value so a reply that never streamed (a plain non-streaming model,
    a segment-less structured output) still reaches the consumer, and the
    usage a session display renders."""

    event_kind: Literal["run_result"] = "run_result"
    output: str | list[MessageSegment] | None = None
    usage: RunUsage


class RunErrorEvent(BaseModel):
    """A turn the graph could not finish, reported to the channel it came from, or
    to the one it was operated from."""

    event_kind: Literal["run_error"] = "run_error"
    message: str
    trace_id: str = Field(description="The trace holding the failure's detail.")


class GatewayEvent(BaseModel):
    """A spell carried the conversation: the console's own operation, or one the
    agent cast itself mid-turn."""

    event_kind: Literal["gateway"] = "gateway"
    action: GatewayAction
    destination: ChannelAddress = Field(
        description="Where the conversation is now; a summon's own thread."
    )
    announcement: str | None = Field(
        default=None,
        description="The line the move leaves where the conversation was, or None "
        "when it leaves none there.",
    )


@dataclass
class ResultSegmentEvent:
    """One completed message segment from a `list[MessageSegment]` output.

    This is a convenience streaming event for channel renderers. The full typed
    output, segment list or otherwise, still arrives as `FinalResult[OutputT]`.
    """

    segment: MessageSegment
    event_kind: Literal["result_segment"] = "result_segment"


@dataclass
class ResultTextDeltaEvent:
    """The trailing text segment of a `list[MessageSegment]` output, as it grows.

    A tool-backed segment reply carries its visible text inside the output
    tool's streamed arguments, where no native text events exist; partial
    validation reveals the trailing segment's text as it grows, and each event
    carries the newly revealed piece so the channel renders the reply while the
    model is still writing it. A segment that streamed this way is settled by
    its last delta and never re-emitted as a `ResultSegmentEvent`.
    """

    delta: str
    event_kind: Literal["result_text_delta"] = "result_text_delta"


class DisplayEvent(BaseModel):
    """Fire-and-forget outbound event; the run continues after it is emitted."""


class TodoCreatedEvent(DisplayEvent):
    """A todo was created."""

    event_kind: Literal["todo_created"] = "todo_created"
    todo: Todo


class TodoUpdatedEvent(DisplayEvent):
    """A todo's content or placement changed."""

    event_kind: Literal["todo_updated"] = "todo_updated"
    todo: Todo
    previous: Todo | None = None


class TodoStatusChangedEvent(DisplayEvent):
    """A todo moved to another status."""

    event_kind: Literal["todo_status_changed"] = "todo_status_changed"
    todo: Todo
    previous: Todo | None = None


class TodoCompletedEvent(DisplayEvent):
    """A todo was completed."""

    event_kind: Literal["todo_completed"] = "todo_completed"
    todo: Todo


class TodoDeletedEvent(DisplayEvent):
    """A todo was deleted."""

    event_kind: Literal["todo_deleted"] = "todo_deleted"
    todo: Todo


# The granular todo events a capability emits as the agent mutates its todo list.
type TodoEvent = (
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
        "already resolved from the target the model named, so the consumer delivers "
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


class LinkProfileAuthorizationEvent(DisplayEvent):
    """A channel requests account authorization from the Octomate host.

    Delivered directly through private feelers, not the agent's run stream. The
    host keeps the request; browser consent links its profile without returning
    an OAuth credential to the channel.
    """

    event_kind: Literal["link_profile_authorization"] = "link_profile_authorization"
    host: str = Field(description="The Octomate host asking for browser consent.")
    authorization: LinkProfileAuthorization


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

    @classmethod
    def from_batch(cls, batch: DeferredActionBatch) -> ActionBatchEvent:
        return cls(
            batch_id=str(batch.id),
            questions=list(batch.questions),
            approvals=list(batch.approvals),
        )


# The stream a consumer matches on, generic over the run's output type.
type StreamEvents[OutputT] = (
    AgentStreamEvent
    | ResultSegmentEvent
    | ResultTextDeltaEvent
    | FinalResult[OutputT]
    | TodoEvent
    | MessageSentEvent
    | OAuthAuthorizationEvent
    | ActionBatchEvent
    | SubagentStartedEvent
    | SubagentSettledEvent
)

# The run stream as a wire consumer sees it: `StreamEvents` with `FinalResult`
# dropped for `RunResultEvent`, plus the graph's own reports, every member
# discriminated by `event_kind`.
type WireEvent = (
    AgentStreamEvent
    | ResultSegmentEvent
    | ResultTextDeltaEvent
    | TodoEvent
    | MessageSentEvent
    | OAuthDeviceAuthorizationEvent
    | OAuthAuthorizationEvent
    | ActionBatchEvent
    | SubagentStartedEvent
    | SubagentSettledEvent
    | RunResultEvent
    | RunErrorEvent
    | GatewayEvent
)

# Serialization-only: wire consumers never validate events back in, so the
# union needs no validation discriminator (the OAuth pair could not carry one
# anyway — both declare the same two-valued `event_kind` literal). Binary
# payloads (a `FilePart`, bytes in a tool return) travel base64 like
# pydantic-ai's own `tool_return_ta` does. Dump with warnings=False: the
# union's Any-typed provider fields (tool return content, provider_details, a
# callable thinking-signature slot) trip the serializer warning on ordinary
# events, which would log once per streamed delta.
wire_event_adapter: TypeAdapter[WireEvent] = TypeAdapter(
    WireEvent,
    config=ConfigDict(ser_json_bytes="base64"),
)
