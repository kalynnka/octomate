from __future__ import annotations

import uuid
from datetime import datetime, timezone

import logfire
from arcanus import RelationCollection
from pydantic_ai.tools import DeferredToolRequests

from octomate.database import async_session
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.schemas.deferred import (
    DeferredAction,
    DeferredActionBatch,
    DeferredActionCollection,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.schemas.triage import ResponseTargetMode, TriageDecision
from octomate.types.deferred import DeferredBatchStatus


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
    ) -> DeferredActionBatch:
        with logfire.span("deferred.create_batch", run_name=run_name) as span:
            actions = DeferredActionCollection.validate_python(requests)
            questions = [
                action for action in actions if isinstance(action, DeferredQuestion)
            ]
            approvals = [
                action for action in actions if isinstance(action, DeferredApproval)
            ]
            batch = DeferredActionBatch(
                conversation_id=conversation.id,
                agent_tentacle_id=agent_tentacle_id,
                run_name=run_name,
                source_key=source_key,
                target_key=target_key,
                target_mode=target_mode,
                decision=decision,
                requests=requests,
                questions=RelationCollection(questions),
                approvals=RelationCollection(approvals),
            )

            span.set_attribute("batch_id", str(batch.id))
            span.set_attribute("questions", len(questions))
            span.set_attribute("approvals", len(approvals))

            async with async_session() as session:
                session.add(batch)
                await session.commit()
                await batch.questions
                await batch.approvals

            async with async_session() as session:
                return await session.one(DeferredActionBatch, id=batch.id)

    async def get_batch(
        self,
        batch_id: uuid.UUID,
    ) -> DeferredActionBatch:
        async with async_session() as session:
            batch = await session.get(DeferredActionBatch, batch_id)
            if batch is None:
                raise ValueError(f"unknown deferred action batch {batch_id}")
            await batch.questions
            await batch.approvals
            return batch

    async def resolve_batch(
        self,
        awake: DeferredActionBatchResponse,
    ) -> DeferredActionBatch:
        with logfire.span(
            "deferred.resolve_batch",
            batch_id=str(awake.batch_id),
            answers=len(awake.answers),
            approvals=len(awake.approvals),
        ) as span:
            async with async_session() as session:
                batch = await session.get(DeferredActionBatch, awake.batch_id)
                if batch is None:
                    raise ValueError(f"unknown deferred action batch {awake.batch_id}")
                await batch.questions
                await batch.approvals
                if batch.status in {"completed", "resuming"}:
                    span.set_attribute("batch.status", batch.status)
                    span.set_attribute("batch.completed", batch.completed)
                    return batch
                questions_by_id = {action.id: action for action in batch.questions}
                approvals_by_id = {action.id: action for action in batch.approvals}
                now = datetime.now(timezone.utc)

                for action_id, answer in awake.answers.items():
                    action = questions_by_id.get(action_id)
                    if action is None:
                        raise ValueError(
                            f"unknown deferred action {action_id} in batch "
                            f"{awake.batch_id}"
                        )
                    if action.status != "pending":
                        continue
                    action.status = "answered"
                    action.result = answer
                    action.responder_id = awake.responder_id or None
                    action.resolved_at = now
                    action.updated_at = now

                for action_id, approved in awake.approvals.items():
                    action = approvals_by_id.get(action_id)
                    if action is None:
                        raise ValueError(
                            f"unknown deferred action {action_id} in batch "
                            f"{awake.batch_id}"
                        )
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

                if batch.completed:
                    batch.status = "resolved"
                batch.updated_at = now
                await session.commit()
                span.set_attribute("batch.status", batch.status)
                span.set_attribute("batch.completed", batch.completed)
                return batch

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
