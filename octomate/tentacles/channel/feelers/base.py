"""Base channel feeler aggregate surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

import logfire
from pydantic_ai.tools import DeferredToolRequests

from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.schemas.deferred import DeferredActionBatch
from octomate.schemas.triage import ResponseTargetMode, TriageDecision
from octomate.tentacles.channel.feelers.deferred import (
    ApprovalFeeler,
    QuestionFeeler,
)
from octomate.tentacles.channel.feelers.output import (
    JsonValue,
    MarkdownFeeler,
    MarkdownStreamFeeler,
    TimelineFeeler,
)

if TYPE_CHECKING:
    from octomate.managers.deferred import DeferredActionManager


OutputT = TypeVar("OutputT", bound=JsonValue | DeferredToolRequests)


@dataclass
class Feelers(Generic[OutputT]):
    """Per-channel human-interaction surface."""

    markdown: MarkdownFeeler
    markdown_stream: MarkdownStreamFeeler[OutputT]
    timeline: TimelineFeeler
    approvals: ApprovalFeeler
    ask_questions: QuestionFeeler

    async def present_actions(
        self,
        *,
        action_manager: DeferredActionManager,
        conversation: Conversation,
        agent_tentacle_id: str,
        run_name: str | None,
        source_key: ConversationKey,
        target_key: ConversationKey,
        target_mode: ResponseTargetMode,
        decision: TriageDecision | None,
        requests: DeferredToolRequests,
    ) -> DeferredActionBatch:
        with logfire.span(
            "present_actions",
            run_name=run_name,
            target_key=str(target_key),
        ) as span:
            batch = await action_manager.create_batch(
                conversation=conversation,
                agent_tentacle_id=agent_tentacle_id,
                run_name=run_name,
                source_key=source_key,
                target_key=target_key,
                target_mode=target_mode,
                decision=decision,
                requests=requests,
            )
            approvals = list(batch.approvals)
            questions = list(batch.questions)
            span.set_attribute("approvals", len(approvals))
            span.set_attribute("questions", len(questions))

            approval_message_ids = await self.approvals.present(target_key, approvals)
            for action in approvals:
                await action_manager.mark_action_presented(
                    action.id,
                    approval_message_ids.get(action.id),
                )

            message_ids = await self.ask_questions.present(target_key, questions)
            for action in questions:
                await action_manager.mark_action_presented(
                    action.id,
                    message_ids.get(action.id),
                )

            return batch
