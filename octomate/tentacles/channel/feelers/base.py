"""Base channel feeler aggregate surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai.tools import DeferredToolRequests

from octomate.schemas.conversation import Conversation, ChannelAddress
from octomate.schemas.deferred import DeferredActionBatch
from octomate.schemas.triage import ResponseTargetMode, SummonDecision
from octomate.telemetry import channel_logfire
from octomate.tentacles.channel.feelers.deferred import (
    ApprovalFeeler,
    QuestionFeeler,
)
from octomate.tentacles.channel.feelers.output import (
    MarkdownFeeler,
    SegmentsFeeler,
    TimelineFeeler,
)

if TYPE_CHECKING:
    from octomate.managers.deferred import DeferredActionManager


@dataclass
class Feelers:
    """Per-channel human-interaction surface."""

    markdown: MarkdownFeeler
    timeline: TimelineFeeler
    segments: SegmentsFeeler
    approvals: ApprovalFeeler
    ask_questions: QuestionFeeler

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

            approval_message_ids = await self.approvals.present(target_address, approvals)
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

            return batch
