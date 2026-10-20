from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from pydantic import UUID7, BaseModel, Field

from octomate.schemas.commands import CommandContext, CommandInvocation
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import QuestionAnswer
from octomate.schemas.events import MessageEvent
from octomate.schemas.triage import SchemeDecision, SummonDecision, TeleportDecision
from octomate.schemas.user import UserProfile


@dataclass(frozen=True)
class UserMessageSignal:
    """Messages using channel routing or a caller-selected agent."""

    messages: list[MessageEvent]
    trigger_thread_message_id: UUID7 | None = None
    agent_id: str | None = None  # Caller-selected executor; otherwise Route selects it.

    def __bool__(self) -> bool:
        return bool(self.messages)

    @cached_property
    def address(self) -> ChannelAddress:
        if not self.messages:
            raise ValueError("empty user message signal has no conversation address")
        last_event = self.messages[-1]
        return ChannelAddress(
            channel_tentacle_id=last_event.tentacle_id,
            chat_type=last_event.chat_type,
            chat_id=last_event.chat_id,
            user_id=last_event.user_id,
            channel_thread_id=last_event.channel_thread_id,
            shared=last_event.shared,
        )


class DeferredActionBatchResponse(BaseModel):
    batch_id: UUID7
    responder_id: str = ""
    answers: dict[UUID7, QuestionAnswer] = Field(default_factory=dict)
    approvals: dict[UUID7, bool] = Field(default_factory=dict)
    allow_session: bool = False


@dataclass(frozen=True)
class NativeGatewaySignal:
    """A native session's own spell, kicked as its own turn.

    A driven turn's decision is read off its Octomate session when the run ends; a
    session running outside Octomate has no run in the graph, so its served
    spell hands the validated decision straight to the graph instead.
    """

    # A native session cannot be summoned: there is nothing here to hand over.
    decision: SchemeDecision | TeleportDecision
    # The native pseudo-channel the handoff is attributed to — its ledger `from` side.
    agent_id: str
    # The registry profile the native id is linked to; None when nobody claims it.
    user_profile: UserProfile | None
    # Where the spell came from: the session's own thread for a teleport, else a
    # pseudo-address on the native id, for the crossing announce to speak to and
    # any failure to land back against.
    source: ChannelAddress | None = None


@dataclass(frozen=True)
class CommandSignal:
    """Explicit command intent on an authenticated, resolved conversation surface."""

    context: CommandContext
    invocation: CommandInvocation
    delivery_id: str  # Stable across retries of the same explicit request.


@dataclass(frozen=True)
class DrivenGatewaySignal:
    """An authenticated, validated operation on an existing thread."""

    thread_id: UUID7
    decision: SummonDecision | TeleportDecision
    agent_id: str
    user_profile: UserProfile
    source: ChannelAddress


@dataclass(frozen=True)
class SummonSignal:
    """Start independent work at a newly opened destination.

    Awake initializes the thread and enters React with the selected agent.
    React records the handoff from this requester before running the agent.
    """

    decision: SummonDecision
    destination: ChannelAddress  # The newly opened thread to initialize.
    agent_id: str  # The requesting agent, kept as the handoff's source.
    user_profile: UserProfile
    source_conversation_id: UUID7 | None = None
    project: str | None = None  # The validated project to bind the new thread to.
    ref: str | None = None  # The validated starting ref for that project's workspace.


type AwakeSignal = (
    UserMessageSignal
    | DeferredActionBatchResponse
    | CommandSignal
    | NativeGatewaySignal
    | DrivenGatewaySignal
    | SummonSignal
)


class StillAwake(RuntimeError):
    """A kick refused because a reflex is still awake on the thread it was asked
    on: a thread holds one at a time."""

    def __init__(self) -> None:
        super().__init__("A turn is still running in this thread.")
