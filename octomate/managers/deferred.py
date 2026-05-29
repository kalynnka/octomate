from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from arcanus import RelationCollection
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from octomate.database import async_session
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.schemas.deferred import (
    DeferredAction,
    DeferredActionBatch,
    DeferredActionCollection,
    DeferredActionVariant,
)
from octomate.schemas.triage import ResponseTargetMode, TriageDecision
from octomate.types.deferred import DeferredBatchStatus


@dataclass(frozen=True)
class DeferredActionContext:
    batch: DeferredActionBatch

    @property
    def actions(self) -> list[DeferredActionVariant]:
        return list(self.batch.actions)

    @property
    def completed(self) -> bool:
        return all(action.resolved for action in self.actions)

    def build_results(self) -> DeferredToolResults:
        results = DeferredToolResults()
        question_actions: dict[str, list[DeferredActionVariant]] = {}
        for action in self.actions:
            if action.kind == "question":
                question_actions.setdefault(action.tool_call_id, []).append(action)
            elif action.kind == "approval":
                results.approvals[action.tool_call_id] = bool(action.result)
            if action.metadata:
                results.metadata[action.tool_call_id] = action.metadata
        for tool_call_id, actions in question_actions.items():
            ordered = sorted(actions, key=lambda action: action.position)
            results.calls[tool_call_id] = [
                "" if action.result is None else str(action.result)
                for action in ordered
            ]
        return results


class DeferredActionManager:
    async def create_batch(
        self,
        *,
        conversation: Conversation,
        agent_tentacle_id: str,
        run_name: str | None,
        source_key: ConversationKey,
        target_key: ConversationKey,
        target_mode: ResponseTargetMode,
        decision: TriageDecision | None,
        requests: DeferredToolRequests,
    ) -> DeferredActionContext:
        actions = DeferredActionCollection.validate_python(requests)
        batch = DeferredActionBatch(
            conversation_id=conversation.id,
            agent_tentacle_id=agent_tentacle_id,
            run_name=run_name,
            source_key=source_key,
            target_key=target_key,
            target_mode=target_mode,
            decision=decision,
            requests=requests,
            actions=RelationCollection(actions),
        )

        async with async_session() as session:
            session.add(batch)
            await session.commit()
            await batch.actions
            return DeferredActionContext(batch=batch)

    async def get_batch_context(
        self,
        batch_id: uuid.UUID,
    ) -> DeferredActionContext:
        async with async_session() as session:
            batch = await session.get(DeferredActionBatch, batch_id)
            if batch is None:
                raise ValueError(f"unknown deferred action batch {batch_id}")
            await batch.actions
            return DeferredActionContext(batch=batch)

    async def resolve_batch(
        self,
        awake: DeferredActionBatchResponse,
    ) -> DeferredActionContext:
        async with async_session() as session:
            batch = await session.get(DeferredActionBatch, awake.batch_id)
            if batch is None:
                raise ValueError(f"unknown deferred action batch {awake.batch_id}")
            await batch.actions
            context = DeferredActionContext(batch=batch)
            if batch.status in {"completed", "resuming"}:
                return context
            actions_by_id = {action.id: action for action in context.actions}
            now = datetime.now(timezone.utc)

            for action_id, answer in awake.answers.items():
                action = actions_by_id.get(action_id)
                if action is None:
                    raise ValueError(
                        f"unknown deferred action {action_id} in batch {awake.batch_id}"
                    )
                if action.kind != "question":
                    raise ValueError(f"deferred action {action_id} is not a question")
                if action.status != "pending":
                    continue
                action.status = "answered"
                action.result = answer
                action.responder_id = awake.responder_id or None
                action.resolved_at = now
                action.updated_at = now

            for action_id, approved in awake.approvals.items():
                action = actions_by_id.get(action_id)
                if action is None:
                    raise ValueError(
                        f"unknown deferred action {action_id} in batch {awake.batch_id}"
                    )
                if action.kind != "approval":
                    raise ValueError(f"deferred action {action_id} is not an approval")
                if action.status != "pending":
                    continue
                action.status = "approved" if approved else "denied"
                action.result = approved
                action.metadata = {
                    **action.metadata,
                    "allow_session": awake.allow_session,
                }
                action.responder_id = awake.responder_id or None
                action.resolved_at = now
                action.updated_at = now

            if all(item.status != "pending" for item in context.batch.actions):
                context.batch.status = "resolved"
            context.batch.updated_at = now
            await session.commit()
            return context

    async def mark_action_presented(
        self,
        action_id: uuid.UUID,
        platform_message_id: str | None,
    ) -> None:
        if not platform_message_id:
            return
        async with async_session() as session:
            action = await session.get(DeferredAction, action_id)
            if action is None:
                return
            action.platform_message_id = platform_message_id
            action.updated_at = datetime.now(timezone.utc)
            await session.commit()

    async def mark_batch(
        self,
        batch_id: uuid.UUID,
        status: DeferredBatchStatus,
        *,
        completed: bool = False,
    ) -> None:
        async with async_session() as session:
            batch = await session.get(DeferredActionBatch, batch_id)
            if batch is None:
                return
            now = datetime.now(timezone.utc)
            batch.status = status
            batch.updated_at = now
            if completed:
                batch.completed_at = now
            await session.commit()
