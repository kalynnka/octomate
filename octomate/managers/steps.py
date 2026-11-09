"""The upstream StepStore contract backed by Octomate's Arcanus tables."""

from __future__ import annotations

from hashlib import sha256

from arcanus import BaseTransmuter
from arcanus.materia.sqlalchemy import AsyncSession, noload
from pydantic import UUID7, ConfigDict, TypeAdapter
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_ai_harness.step_persistence import (
    ContinuableSnapshot,
    RunRecord,
    StepEvent,
    ToolEffectRecord,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Insert as PostgresInsert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import Insert as SqliteInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from octomate.database import async_session
from octomate.managers.base import Manager
from octomate.schemas.conversation import ConversationRun
from octomate.schemas.runs import AgentRun
from octomate.schemas.steps import (
    Snapshot,
    SnapshotMembership,
    SnapshotMessage,
    ToolEffect,
)
from octomate.schemas.steps import (
    StepEvent as StoredStepEvent,
)

conversation_id_adapter: TypeAdapter[UUID7] = TypeAdapter(UUID7)
message_adapter: TypeAdapter[ModelMessage] = TypeAdapter(
    ModelMessage, config=ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")
)
event_adapter: TypeAdapter[StepEvent] = TypeAdapter(StepEvent)
effect_adapter: TypeAdapter[ToolEffectRecord] = TypeAdapter(ToolEffectRecord)


def conflict_insert(
    session: AsyncSession, entity: type[BaseTransmuter]
) -> SqliteInsert | PostgresInsert:
    """Use the database's atomic conflict handling for retried harness writes."""
    return {"sqlite": sqlite_insert, "postgresql": postgres_insert}[
        session.get_bind().dialect.name
    ](entity)


class StepStore(Manager):
    """Run registration, checkpoints, events and tool effects for agent execution.

    A registration must name an existing Octomate conversation UUID; detached
    harness runs have no owner in this service. Checkpoints preserve upstream
    message values, including compacted contexts, without moving archive rows.
    """

    async def register_run(self, record: RunRecord) -> None:
        conversation_id = conversation_id_adapter.validate_python(
            record.conversation_id
        )
        run = AgentRun(
            id=record.run_id,
            conversation_id=conversation_id,
            parent_run_id=record.parent_run_id,
            started_at=record.started_at,
            agent_name=record.agent_name,
            meta=record.metadata,
            registration_id=record.registration_id,
        )
        async with async_session() as session:
            session.add(run)
            await session.flush()
            session.add(ConversationRun(conversation_id=conversation_id, run_id=run.id))
            await session.commit()

    async def get_run(self, *, run_id: str) -> RunRecord | None:
        async with async_session() as session:
            row = await session.one_or_none(
                AgentRun,
                expressions=[
                    AgentRun["id"] == run_id,
                    AgentRun["metadata"].is_not(None),
                ],
                options=[noload(AgentRun["messages"])],
            )
            return self.run_record(row) if row is not None else None

    async def list_runs(
        self, *, parent_run_id: str | None = None, conversation_id: str | None = None
    ) -> list[RunRecord]:
        statement = select(AgentRun).where(AgentRun["metadata"].is_not(None))
        if parent_run_id is not None:
            statement = statement.where(AgentRun["parent_run_id"] == parent_run_id)
        if conversation_id is not None:
            statement = statement.where(
                AgentRun["conversation_id"]
                == conversation_id_adapter.validate_python(conversation_id)
            )
        statement = statement.order_by(AgentRun["started_at"], AgentRun["id"]).options(
            noload(AgentRun["messages"])
        )
        async with async_session() as session:
            rows = (await session.scalars(statement)).all()
            return [self.run_record(row) for row in rows]

    @staticmethod
    def run_record(run: AgentRun) -> RunRecord:
        """Expose a registered service run through the upstream store contract."""
        if run.started_at is None or run.metadata is None:
            raise ValueError("A registered run must have a start time and metadata")
        return RunRecord(
            run_id=run.id,
            conversation_id=str(run.conversation_id),
            parent_run_id=run.parent_run_id,
            agent_name=run.agent_name,
            metadata=run.metadata,
            started_at=run.started_at,
            registration_id=run.registration_id,
        )

    async def append_event(self, event: StepEvent) -> None:
        row = StoredStepEvent.model_validate(vars(event))
        async with async_session() as session:
            await session.execute(
                conflict_insert(session, StoredStepEvent)
                .values(**row.model_dump(by_alias=True, exclude={"seq"}))
                .on_conflict_do_nothing(index_elements=["run_id", "idempotency_key"])
            )
            await session.commit()

    async def list_events(self, *, run_id: str) -> list[StepEvent]:
        async with async_session() as session:
            rows = await session.list(
                StoredStepEvent,
                limit=None,
                expressions=[StoredStepEvent["run_id"] == run_id],
                order_bys=[StoredStepEvent["seq"]],
            )
            return [event_adapter.validate_python(row.model_dump()) for row in rows]

    async def save_snapshot(self, snapshot: ContinuableSnapshot) -> None:
        row = Snapshot.model_validate(vars(snapshot))
        async with async_session() as session:
            stored = (
                await session.scalars(
                    conflict_insert(session, Snapshot)
                    .values(**row.model_dump(exclude={"seq"}))
                    .on_conflict_do_nothing(
                        index_elements=["run_id", "idempotency_key"]
                    )
                    .returning(Snapshot)
                )
            ).one_or_none()
            if stored is None:
                return
            assert stored.seq is not None
            contents: dict[str, bytes] = {}
            members: list[SnapshotMembership] = []
            for position, message in enumerate(snapshot.messages):
                content = message_adapter.dump_json(message)
                digest = sha256(content).hexdigest()
                contents[digest] = content
                members.append(
                    SnapshotMembership(
                        snapshot_seq=stored.seq,
                        position=position,
                        message_sha256=digest,
                    )
                )
            if contents:
                known = set(
                    (
                        await session.scalars(
                            select(SnapshotMessage["sha256"]).where(
                                SnapshotMessage["sha256"].in_(contents)
                            )
                        )
                    ).all()
                )
                missing = [
                    {"sha256": digest, "content": content}
                    for digest, content in contents.items()
                    if digest not in known
                ]
                if missing:
                    await session.execute(
                        conflict_insert(
                            session, SnapshotMessage
                        ).on_conflict_do_nothing(index_elements=["sha256"]),
                        missing,
                    )
                session.add_all(members)
            await session.commit()

    async def latest_snapshot(
        self, *, run_id: str, include_interrupted: bool = False
    ) -> ContinuableSnapshot | None:
        expressions = [Snapshot["run_id"] == run_id]
        if not include_interrupted:
            expressions.append(Snapshot["state"] == "complete")
        async with async_session() as session:
            row = await session.first(
                Snapshot,
                expressions=expressions,
                order_bys=[Snapshot["seq"].desc()],
            )
            if row is None:
                return None
            contents = (
                await session.scalars(
                    select(SnapshotMessage["content"])
                    .select_from(SnapshotMembership)
                    .join(SnapshotMembership["message"])
                    .where(SnapshotMembership["snapshot_seq"] == row.seq)
                    .order_by(SnapshotMembership["position"])
                )
            ).all()
            return ContinuableSnapshot(
                **row.model_dump(exclude={"seq"}),
                messages=ModelMessagesTypeAdapter.validate_json(
                    b"[" + b",".join(contents) + b"]"
                ),
            )

    async def record_tool_effect(self, record: ToolEffectRecord) -> None:
        row = ToolEffect.model_validate(vars(record))
        async with async_session() as session:
            await session.execute(
                conflict_insert(session, ToolEffect)
                .values(**row.model_dump())
                .on_conflict_do_update(
                    index_elements=["run_id", "tool_call_id"],
                    set_=row.model_dump(exclude={"run_id", "tool_call_id"}),
                )
            )
            await session.commit()

    async def get_tool_effect(
        self, *, run_id: str, tool_call_id: str
    ) -> ToolEffectRecord | None:
        async with async_session() as session:
            row = await session.get(ToolEffect, (run_id, tool_call_id))
            return (
                effect_adapter.validate_python(row.model_dump())
                if row is not None
                else None
            )

    async def list_unresolved_tool_effects(
        self, *, run_id: str
    ) -> list[ToolEffectRecord]:
        async with async_session() as session:
            rows = await session.list(
                ToolEffect,
                limit=None,
                expressions=[
                    ToolEffect["run_id"] == run_id,
                    ToolEffect["status"] == "started",
                ],
                order_bys=[
                    ToolEffect["started_at"],
                    ToolEffect["tool_call_id"],
                ],
            )
            return [effect_adapter.validate_python(row.model_dump()) for row in rows]
