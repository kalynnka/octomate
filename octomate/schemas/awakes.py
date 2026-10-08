from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from pydantic import UUID7, BaseModel, Field

from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import QuestionAnswer
from octomate.schemas.events import MessageEvent
from octomate.schemas.triage import SchemeDecision, SummonDecision, TeleportDecision
from octomate.schemas.user import UserProfile


@dataclass(frozen=True)
class UserMessageSignal:
    messages: list[MessageEvent]
    trigger_thread_message_id: UUID7 | None = None

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
    native session has no run in the graph, so the served spell hands its
    validated decision straight to the graph instead.
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
class DrivenGatewaySignal:
    """An authenticated, validated operation on an existing thread."""

    thread_id: UUID7
    decision: SummonDecision | TeleportDecision
    agent_id: str
    user_profile: UserProfile
    source: ChannelAddress
    # The channel the operation was performed in, which hears how it goes.
    operated_from: str


type AwakeSignal = (
    UserMessageSignal
    | DeferredActionBatchResponse
    | NativeGatewaySignal
    | DrivenGatewaySignal
)
