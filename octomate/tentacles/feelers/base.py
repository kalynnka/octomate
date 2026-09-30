"""Base channel feeler aggregate surface."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic_ai.tools import DeferredToolRequests

from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.deferred import DeferredActionBatch
from octomate.schemas.triage import ResponseTargetMode, SummonDecision
from octomate.telemetry import channel_logfire
from octomate.tentacles.feelers.deferred import (
    ApprovalFeeler,
    QuestionFeeler,
)
from octomate.tentacles.feelers.oauth import OAuthFeeler
from octomate.tentacles.feelers.output import (
    MarkdownFeeler,
    SegmentsFeeler,
    TimelineFeeler,
    TimelineState,
)

if TYPE_CHECKING:
    from octomate.managers.deferred import DeferredActionManager

logger = logging.getLogger(__name__)


@dataclass
class Feelers:
    """Per-channel human-interaction surface."""

    markdown: MarkdownFeeler
    timeline: TimelineFeeler
    segments: SegmentsFeeler
    approvals: ApprovalFeeler
    ask_questions: QuestionFeeler
    oauth: OAuthFeeler
    # The timeline a live run stream is rendering onto, keyed by str(address).
    # Registered around `drive` so a batch presented from an agent's in-process
    # bridge — outside the stream — can settle that surface (see
    # `TimelineState.actions_presented`).
    live_timelines: dict[str, TimelineState] = field(default_factory=dict)

    @asynccontextmanager
    async def driving(
        self, address: ChannelAddress, timeline: TimelineState
    ) -> AsyncGenerator[None]:
        """Marks `timeline` as the surface rendering `address`'s live run."""
        key = str(address)
        self.live_timelines[key] = timeline
        try:
            yield
        finally:
            # A callback already settling the surface must finish before the
            # timeline's context closes its streams/cards.
            async with timeline.render_lock:
                if self.live_timelines.get(key) is timeline:
                    del self.live_timelines[key]

    async def present_actions(
        self,
        *,
        action_manager: DeferredActionManager,
        conversation: Conversation,
        agent_tentacle_id: str,
        run_name: str | None,
        source_address: ChannelAddress,
        target_address: ChannelAddress,
        target_mode: ResponseTargetMode,
        decision: SummonDecision | None,
        requests: DeferredToolRequests,
    ) -> DeferredActionBatch:
        with channel_logfire.span(
            "present_actions",
            run_name=run_name,
            target_address=str(target_address),
        ) as span:
            batch = await action_manager.create_batch(
                conversation=conversation,
                agent_tentacle_id=agent_tentacle_id,
                run_name=run_name,
                source_address=source_address,
                target_address=target_address,
                target_mode=target_mode,
                decision=decision,
                requests=requests,
            )
            approvals = list(batch.approvals)
            questions = list(batch.questions)
            span.set_attribute("approvals", len(approvals))
            span.set_attribute("questions", len(questions))

            approval_message_ids = await self.approvals.present(
                target_address, approvals
            )
            for action in approvals:
                await action_manager.mark_action_presented(
                    action.id,
                    approval_message_ids.get(action.id),
                )

            message_ids = await self.ask_questions.present(target_address, questions)
            for action in questions:
                await action_manager.mark_action_presented(
                    action.id,
                    message_ids.get(action.id),
                )

            await self.settle_timeline(target_address)
            return batch

    async def settle_timeline(self, address: ChannelAddress) -> None:
        """Settle a live surface between events, before its resources close."""
        key = str(address)
        timeline = self.live_timelines.get(key)
        if timeline is None:
            return
        # The cards are already up; a render hiccup must not cancel the approval.
        try:
            async with timeline.render_lock:
                if self.live_timelines.get(key) is not timeline:
                    return
                await timeline.actions_presented()
        except Exception:
            logger.warning(
                "live timeline for %s failed to settle after presenting actions",
                address,
                exc_info=True,
            )
