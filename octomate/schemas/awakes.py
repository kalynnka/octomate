from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import cached_property
from typing import TypeAlias

from pydantic import BaseModel, Field

from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.events import MessageEvent
from octomate.schemas.triage import SchemeDecision, SummonDecision
from octomate.schemas.user import UserProfile


@dataclass(frozen=True)
class UserMessageSignal:
    messages: list[MessageEvent]
    trigger_thread_message_id: uuid.UUID | None = None

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
    batch_id: uuid.UUID
    responder_id: str = ""
    answers: dict[uuid.UUID, str] = Field(default_factory=dict)
    approvals: dict[uuid.UUID, bool] = Field(default_factory=dict)
    allow_session: bool = False


@dataclass(frozen=True)
class GatewayHandoffSignal:
    """A native session's summon or scheme, kicked as its own turn.

    A driven turn's decision is read off its gateway session when the run ends; an
    anonymous native session has no run in the graph, so the served spell hands its
    validated decision straight to the graph instead.
    """

    decision: SummonDecision | SchemeDecision
    # The native pseudo-channel the handoff is attributed to — its ledger `from` side.
    agent_id: str
    # The registry profile the native id is linked to; None when nobody claims it.
    user_profile: UserProfile | None
    # Where the handoff came from: a pseudo-address on the native id, for the
    # crossing announce to speak to and any failure to land back against.
    source: ChannelAddress | None = None


AwakeSignal: TypeAlias = (
    UserMessageSignal | DeferredActionBatchResponse | GatewayHandoffSignal
)
