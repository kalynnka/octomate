from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from arcanus import RelationCollection
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from octomate.database import async_session
from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.schemas.deferred import (
    DeferredAction,
    DeferredActionBatch,
)
from octomate.schemas.awakes import DeferredActionResponse
from octomate.schemas.triage import ResponseTargetMode, TriageDecision
from octomate.types.deferred import DeferredBatchStatus


@dataclass(frozen=True)
class DeferredActionContext:
    batch: DeferredActionBatch

    @property
    def actions(self) -> list[DeferredAction]:
        return list(self.batch.actions)

    @property
    def is_complete(self) -> bool:
        return all(action.is_resolved for action in self.actions)

    def build_results(self) -> DeferredToolResults:
        results = DeferredToolResults()
        for action in self.actions:
            if action.kind == "call":
                results.calls[action.tool_call_id] = action.result
            else:
                results.approvals[action.tool_call_id] = bool(action.result)
            if action.metadata:
                results.metadata[action.tool_call_id] = action.metadata
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
        batch = DeferredActionBatch(
            conversation_id=conversation.id,
            agent_tentacle_id=agent_tentacle_id,
            run_name=run_name,
            source_key=source_key,
            target_key=target_key,
            target_mode=target_mode,
            decision=decision,
            requests=requests,
            actions=RelationCollection(
                [
                    DeferredAction(
                        kind="call",
                        tool_name=call.tool_name,
                        tool_call_id=call.tool_call_id,
                        args=call.args_as_dict(),
                        metadata=requests.metadata.get(call.tool_call_id, {}),
                    )
                    for call in requests.calls
                ]
                + [
                    DeferredAction(
                        kind="approval",
                        tool_name=call.tool_name,
                        tool_call_id=call.tool_call_id,
                        args=call.args_as_dict(),
                        metadata=requests.metadata.get(call.tool_call_id, {}),
                    )
                    for call in requests.approvals
                ]
            ),
        )

        async with async_session() as session:
            session.add(batch)
            await session.commit()
            await batch.actions
            return DeferredActionContext(batch=batch)

    async def get_action_context(
        self,
        action_id: uuid.UUID,
    ) -> DeferredActionContext:
        async with async_session() as session:
            action = await session.get(DeferredAction, action_id)
            if action is None:
                raise ValueError(f"unknown deferred action {action_id}")
            batch = await action.batch
            await batch.actions
            return DeferredActionContext(batch=batch)

    async def resolve_action(
        self,
        awake: DeferredActionResponse,
    ) -> DeferredActionContext:
        async with async_session() as session:
            action = await session.get(DeferredAction, awake.action_id)
            if action is None:
                raise ValueError(f"unknown deferred action {awake.action_id}")
            batch = await action.batch
            await batch.actions
            context = DeferredActionContext(batch=batch)
            if action.status == "pending":
                now = datetime.now(timezone.utc)
                if action.kind == "call":
                    action.status = "answered"
                    action.result = awake.answer or ""
                else:
                    approved = bool(awake.approved)
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
