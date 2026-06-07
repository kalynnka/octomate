"""Typed event stream the agent run produces and channels consume.

One stream, with these event families:

- **Pydantic AI passthrough** (`AgentStreamEvent`) — thinking + tool-call events.
- **Output events** — the agent's reply, generic over the run's output type:
  `OutputDeltaEvent[OutputT]` (partial, validated with `allow_partial=True`) while
  the reply streams, then Pydantic AI's own `FinalResult[OutputT]` as the final
  value — no wrapper of our own. Rendering the output (segments → IM message, rows
  → table, …) is a *consumer* concern; the event just carries the typed value.
- **Display events** (`DisplayEvent`) — fire-and-forget, e.g. `TodoListEvent`.
- **Action requests** (`ActionRequestEvent`) — need a user reply; the run suspends.
  `action_id`/`batch_id` correlate the reply through the deferred-action machinery.

Display and action events are emitted by capabilities (a capability bundles a tool
+ instructions + `wrap_run_event_stream`); the output events are emitted by
`Agent.stream_events` (see octomate/capabilities/agent.py).

Serialization (a wire format for dev_ui) is intentionally not modelled here yet:
the output events are generic, so a single discriminated `TypeAdapter` no longer
fits — that belongs with the consumer/transport layer when dev_ui adopts this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel
from pydantic_ai import AgentStreamEvent
from pydantic_ai.result import FinalResult
from typing_extensions import TypeAliasType

from octomate.schemas.deferred import ApprovalRequest, QuestionRequest

OutputT = TypeVar("OutputT")


@dataclass
class OutputDeltaEvent(Generic[OutputT]):
    """A partial, validated snapshot of the agent's output as it streams."""

    output: OutputT
    event_kind: Literal["output_delta"] = "output_delta"


class TodoItem(BaseModel):
    content: str
    status: Literal["pending", "in_progress", "completed"] = "pending"


class DisplayEvent(BaseModel):
    """Fire-and-forget outbound event; the run continues after it is emitted."""


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


# The stream a consumer matches on, generic over the run's output type.
# (TypeAliasType backports PEP 695's generic alias to the project's 3.11 floor.)
StreamEvents = TypeAliasType(
    "StreamEvents",
    AgentStreamEvent
    | OutputDeltaEvent[OutputT]
    | FinalResult[OutputT]
    | TodoListEvent
    | AskQuestionEvent
    | ApprovalRequestEvent,
    type_params=(OutputT,),
)
