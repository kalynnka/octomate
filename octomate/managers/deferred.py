"""Persistence of deferred-action batches — the questions and approvals a run parks
for a human — and their resolution from replies."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from arcanus import RelationCollection
from arcanus.materia.sqlalchemy import noload
from pydantic import UUID7
from pydantic_ai.tools import DeferredToolRequests
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.events import ActionBatchUpdatedEvent
from octomate.database import async_session
from octomate.managers.base import Manager
from octomate.nerve import Impulse, Nerve
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.deferred import (
    DeferredAction,
    DeferredActionBatch,
    DeferredActionCollection,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.schemas.triage import ResponseTargetMode, SummonDecision
from octomate.telemetry import deferred_logfire
from octomate.types.deferred import DeferredBatchStatus, DeferredResponseMode


@dataclass
class DeferredActionManager(Manager):
    """Persists deferred-action batches and resolves them from user replies."""

    nerve: Nerve = field(default_factory=Nerve)

    async def publish_update(self, batch: DeferredActionBatch) -> None:
        """Announce committed state to every existing reader of this batch."""
        async with self.nerve.publish(batch.source_address) as stream:
            await stream.send(
                Impulse(
                    ActionBatchUpdatedEvent(
                        batch_id=str(batch.id),
                        closed=batch.closed,
                        questions=list(batch.questions),
                        approvals=list(batch.approvals),
                    ),
                    batch.target_address,
                )
            )

    async def create_batch(
        self,
        *,
        conversation: Conversation,
        agent_tentacle_id: str,
        run_name: str | None,
        source_address: ChannelAddress,
        target_address: ChannelAddress,
        target_mode: ResponseTargetMode,
        decision: SummonDecision | None,
        requests: DeferredToolRequests,
        response_mode: DeferredResponseMode,
        batch_id: UUID7 | None = None,
    ) -> DeferredActionBatch:
        with deferred_logfire.span("deferred.create_batch", run_name=run_name) as span:
            actions = DeferredActionCollection.validate_python(requests)
            questions = [
                action for action in actions if isinstance(action, DeferredQuestion)
            ]
            approvals = [
                action for action in actions if isinstance(action, DeferredApproval)
            ]
            batch = DeferredActionBatch(
                id=batch_id or uuid7(),
                response_mode=response_mode,
                conversation_id=conversation.id,
                agent_tentacle_id=agent_tentacle_id,
                run_name=run_name,
                source_address=source_address,
                target_address=target_address,
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

    async def pending_for_thread(
        self,
        thread_id: UUID7,
    ) -> list[DeferredActionBatch]:
        """The thread's unanswered batches, oldest first, so a channel can
        re-present waiting questions and approvals when the thread is reloaded."""
        async with async_session() as session:
            conversations = await session.list(
                Conversation,
                # Only the ids are wanted. Both of these are lazy="selectin", so
                # without the suppression this walks every run and every model
                # message in the thread to collect a handful of uuids.
                options=[
                    noload(Conversation["messages"]),
                    noload(Conversation["runs"]),
                ],
                expressions=[Conversation["thread_id"] == thread_id],
            )
            conversation_ids = [conversation.id for conversation in conversations]
            if not conversation_ids:
                return []
            # questions/approvals are lazy="selectin": the list query loads them
            # in one batched pass, so no per-row loading is needed here.
            batches = await session.list(
                DeferredActionBatch,
                order_bys=[DeferredActionBatch["id"]],
                expressions=[
                    DeferredActionBatch["conversation_id"].in_(conversation_ids),
                    DeferredActionBatch["status"] == "pending",
                ],
            )
        return list(batches)

    async def get_batch(
        self,
        batch_id: UUID7,
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
        *,
        finalize: bool = True,
    ) -> DeferredActionBatch:
        """Apply replies, allowing question answers to change while the batch is open.

        With finalize=False, save question drafts without marking them answered;
        the owner validates them before calling submit_questions.
        """
        with deferred_logfire.span(
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
                if batch.status in {"resolved", "completed", "resuming"}:
                    span.set_attribute("batch.status", batch.status)
                    span.set_attribute("batch.completed", batch.completed)
                    return batch
                if batch.status in {"expired", "superseded", "failed"}:
                    raise ValueError(f"batch already {batch.status}")
                questions_by_id = {action.id: action for action in batch.questions}
                approvals_by_id = {action.id: action for action in batch.approvals}
                now = datetime.now(UTC)

                for action_id, answer in awake.answers.items():
                    action = questions_by_id.get(action_id)
                    if action is None:
                        raise ValueError(
                            f"unknown deferred action {action_id} in batch "
                            f"{awake.batch_id}"
                        )
                    if action.status not in {"pending", "answered"}:
                        continue
                    action.status = "answered" if finalize else "pending"
                    args = action.args.copy()
                    args.pop("error", None)
                    action.args = args
                    action.result = answer
                    action.responder_id = awake.responder_id or None
                    action.resolved_at = now if finalize else None
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
            if batch.closed:
                await self.publish_update(batch)
            return batch

    async def review_questions(
        self, batch_id: UUID7, *, errors: dict[UUID7, str], reset: UUID7
    ) -> DeferredActionBatch:
        """Keep invalid fields editable and require an explicit resubmission."""
        async with async_session() as session:
            batch = await session.one(DeferredActionBatch, id=batch_id)
            if batch.closed:
                raise ValueError(f"batch already {batch.status}")
            await batch.questions
            now = datetime.now(UTC)
            for action in batch.questions:
                args = action.args.copy()
                args.pop("error", None)
                if action.id in errors:
                    args["error"] = errors[action.id]
                action.args = args
                action.updated_at = now
                if action.id == reset:
                    action.result = None
            batch.updated_at = now
            await session.commit()
        await self.publish_update(batch)
        return batch

    async def submit_questions(self, batch_id: UUID7) -> DeferredActionBatch:
        """Finalize an owner's validated draft answers, including omitted fields."""
        async with async_session() as session:
            batch = await session.one(DeferredActionBatch, id=batch_id)
            if batch.closed:
                raise ValueError(f"batch already {batch.status}")
            await batch.questions
            now = datetime.now(UTC)
            for action in batch.questions:
                action.status = "answered"
                action.resolved_at = now
                action.updated_at = now
            batch.status = "resolved"
            batch.updated_at = now
            await session.commit()
        await self.publish_update(batch)
        return batch

    async def mark_action_presented(
        self,
        action_id: UUID7,
        platform_message_id: str | None,
    ) -> None:
        if not platform_message_id:
            return
        async with async_session() as session:
            action = await session.get(DeferredAction, action_id)
            if action is None:
                return
            action.platform_message_id = platform_message_id
            action.updated_at = datetime.now(UTC)
            await session.commit()
            batch_id = action.batch_id
        if batch_id is not None:
            batch = await self.get_batch(batch_id)
            # The request can close while its initial card is still being sent.
            if batch.closed:
                await self.publish_update(batch)

    async def mark_batch(
        self,
        batch_id: UUID7,
        status: DeferredBatchStatus,
        *,
        completed: bool = False,
    ) -> None:
        async with async_session() as session:
            batch = await session.get(DeferredActionBatch, batch_id)
            if batch is None:
                return
            now = datetime.now(UTC)
            batch.status = status
            batch.updated_at = now
            if completed:
                batch.completed_at = now
            if status in {"completed", "expired", "superseded", "failed"}:
                await batch.questions
                await batch.approvals
                pending = [
                    action
                    for action in [*batch.questions, *batch.approvals]
                    if action.status == "pending"
                ]
                for action in pending:
                    action.status = "failed" if status == "failed" else "expired"
                    action.resolved_at = now
                    action.updated_at = now
            await session.commit()
        if batch.closed:
            await self.publish_update(batch)

    async def expire_live(self) -> None:
        """Expire every live request still pending. Its waiter lived in the process
        that asked, so after a restart a reply has nowhere to go; expired, the reply
        is refused rather than accepted and lost."""
        async with async_session() as session:
            batches = await session.list(
                DeferredActionBatch,
                expressions=[
                    DeferredActionBatch["response_mode"] == "live",
                    DeferredActionBatch["status"] == "pending",
                ],
            )
        for batch in batches:
            await self.mark_batch(batch.id, "expired")
