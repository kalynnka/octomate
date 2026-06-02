from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic_ai.tools import DeferredToolRequests

from octomate.managers.conversations import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.triage import ResponseTargetMode, TriageDecision
from octomate.tentacles.channel.base import ChannelTentacle


class DeferredSuspender(Protocol):
    """Human-in-the-loop hook for deferred tool calls the agent cannot resolve
    in process. Unlike `DeferredResolver`, which returns results and lets the
    react loop continue, a suspender persists + presents the requests for an
    out-of-band response and the run ends; it is resumed later by feeding the
    collected `DeferredToolResults` back through a fresh agent run.
    """

    async def suspend(self, requests: DeferredToolRequests) -> None: ...


@dataclass
class HumanReviewSuspender:
    """`DeferredSuspender` that persists a batch and presents it via the
    channel for human review, then leaves the run suspended. Triage builds it
    with the right run context; react invokes it (via `ResolveDeferred`) when an
    agent run yields `DeferredToolRequests` and no in-process resolver is set.
    """

    channel: ChannelTentacle
    action_manager: DeferredActionManager
    conversation_manager: ConversationManager
    agent_tentacle_id: str
    run_name: Literal["triage", "reception"]
    source_key: ConversationKey
    target_key: ConversationKey
    target_mode: ResponseTargetMode
    decision: TriageDecision | None
    suspended_batch_id: uuid.UUID | None = field(default=None, init=False)

    async def suspend(self, requests: DeferredToolRequests) -> None:
        conversation = await self.conversation_manager.ensure(
            self.target_key,
            agent_tentacle_id=self.agent_tentacle_id,
        )
        batch = await self.channel.feelers.present_actions(
            action_manager=self.action_manager,
            conversation=conversation,
            agent_tentacle_id=self.agent_tentacle_id,
            run_name=self.run_name,
            source_key=self.source_key,
            target_key=self.target_key,
            target_mode=self.target_mode,
            decision=self.decision,
            requests=requests,
        )
        self.suspended_batch_id = batch.id
