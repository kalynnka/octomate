"""Channel feelers for human-in-the-loop deferred actions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic_ai.tools import DeferredToolRequests

from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.schemas.deferred import (
    DeferredActionBatch,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.schemas.triage import ResponseTargetMode, TriageDecision

if TYPE_CHECKING:
    from octomate.managers.deferred import DeferredActionManager


TextResponder = Callable[[ConversationKey, str], Awaitable[None]]


class ApprovalFeeler(ABC):
    """Presents approval actions for one response target."""

    @abstractmethod
    async def present(
        self,
        key: ConversationKey,
        actions: list[DeferredApproval],
    ) -> dict[UUID, str | None]: ...


class AskQuestionFeeler(ABC):
    """Presents a card-based question wizard for one response target."""

    @abstractmethod
    async def present(
        self,
        key: ConversationKey,
        actions: list[DeferredQuestion],
    ) -> dict[UUID, str | None]: ...


@dataclass
class Feelers:
    """Per-channel human-interaction surface."""

    approvals: ApprovalFeeler
    ask_questions: AskQuestionFeeler

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
        approval_message_ids = await self.approvals.present(target_key, approvals)
        for action in approvals:
            await action_manager.mark_action_presented(
                action.id,
                approval_message_ids.get(action.id),
            )

        questions = list(batch.questions)
        message_ids = await self.ask_questions.present(target_key, questions)
        for action in questions:
            await action_manager.mark_action_presented(
                action.id,
                message_ids.get(action.id),
            )

        return batch


class PlainTextApprovalFeeler(ApprovalFeeler):
    def __init__(self, respond_text: TextResponder) -> None:
        self.respond_text = respond_text

    async def present(
        self,
        key: ConversationKey,
        actions: list[DeferredApproval],
    ) -> dict[UUID, str | None]:
        for action in actions:
            await self.respond_text(
                key,
                (
                    f"Octomate needs approval for `{action.tool_name}` "
                    f"({action.id}). This channel can show the request, but does "
                    "not support interactive approval cards yet."
                ),
            )
        return {action.id: None for action in actions}


class PlainTextAskQuestionFeeler(AskQuestionFeeler):
    def __init__(self, respond_text: TextResponder) -> None:
        self.respond_text = respond_text

    async def present(
        self,
        key: ConversationKey,
        actions: list[DeferredQuestion],
    ) -> dict[UUID, str | None]:
        for action in actions:
            choices_text = ""
            if choices := action.args.get("choices"):
                choices_text = "\nChoices:\n" + "\n".join(
                    f"- {choice}" for choice in choices
                )
            hint = action.args.get("hint") or ""
            hint_text = f"\nHint: {hint}" if hint else ""
            await self.respond_text(
                key,
                (
                    f"Octomate needs an answer: {action.args['question']}"
                    f"{hint_text}{choices_text}\nDeferred action: `{action.id}`"
                ),
            )
        return {action.id: None for action in actions}
