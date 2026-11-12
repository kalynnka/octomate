"""Arcanus storage types for the StepStore."""

from __future__ import annotations

from typing import Annotated

from arcanus import BaseTransmuter, Identity, Relation, Relationship
from pydantic import AwareDatetime, ConfigDict, Field
from pydantic_ai_harness.step_persistence import (
    EventKind,
    SnapshotState,
    ToolEffectStatus,
)

from octomate.models import steps as models
from octomate.schemas.base import sqlalchemy_materia


@sqlalchemy_materia.bless(models.StepEvent)
class StepEvent(BaseTransmuter):
    """A boundary event; sequence, rather than timestamp, defines append order."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    seq: Annotated[int | None, Identity] = None
    run_id: str
    kind: EventKind
    step_index: int
    timestamp: AwareDatetime
    conversation_id: str | None = None
    parent_run_id: str | None = None
    agent_name: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    error: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict, alias="meta")
    idempotency_key: str | None = None


@sqlalchemy_materia.bless(models.Snapshot)
class Snapshot(BaseTransmuter):
    """Checkpoint metadata; ordered contents live in separate immutable rows."""

    model_config = ConfigDict(from_attributes=True)

    seq: Annotated[int | None, Identity] = None
    run_id: str
    step_index: int
    conversation_id: str | None = None
    parent_run_id: str | None = None
    agent_name: str | None = None
    timestamp: AwareDatetime
    state: SnapshotState = "complete"
    idempotency_key: str | None = None


@sqlalchemy_materia.bless(models.SnapshotMessage)
class SnapshotMessage(BaseTransmuter):
    """Serialized upstream message content, addressed by its digest."""

    model_config = ConfigDict(from_attributes=True)

    sha256: Annotated[str, Identity] = Field(min_length=64, max_length=64)
    content: bytes


@sqlalchemy_materia.bless(models.SnapshotMembership)
class SnapshotMembership(BaseTransmuter):
    """Position of a message version within a checkpoint."""

    model_config = ConfigDict(from_attributes=True)

    snapshot_seq: Annotated[int, Identity]
    position: Annotated[int, Identity] = Field(ge=0)
    message_sha256: str
    message: Relation[SnapshotMessage] = Relationship()


@sqlalchemy_materia.bless(models.ToolEffect)
class ToolEffect(BaseTransmuter):
    """A tool invocation's latest effect status, scoped to its owning run."""

    model_config = ConfigDict(from_attributes=True)

    run_id: Annotated[str, Identity]
    tool_call_id: Annotated[str, Identity]
    tool_name: str
    status: ToolEffectStatus
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    idempotency_key: str | None = None
    effect_summary: str | None = None
