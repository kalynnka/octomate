from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from arcanus import (
    BaseTransmuter,
    Relation,
    RelationCollection,
    Relationship,
    Relationships,
)
from arcanus.base import Identity
from pydantic import AliasChoices, ConfigDict, Field, JsonValue
from pydantic_ai.tools import DeferredToolRequests
from uuid_utils.compat import uuid7

from octomate.models import deferred as deferred_models
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.triage import ResponseTargetMode, TriageDecision
from octomate.types.deferred import (
    DeferredActionKind,
    DeferredActionStatus,
    DeferredBatchStatus,
)


@sqlalchemy_materia.bless(deferred_models.DeferredAction)
class DeferredAction(BaseTransmuter):
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
    )

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    batch_id: uuid.UUID | None = None
    kind: DeferredActionKind
    status: DeferredActionStatus = "pending"
    tool_name: str
    tool_call_id: str
    args: dict[str, JsonValue]
    metadata: dict[str, JsonValue] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("meta", "metadata"),
    )
    result: JsonValue = None
    platform_message_id: str | None = None
    responder_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None

    batch: Relation[DeferredActionBatch] = Relationship()

    @property
    def is_resolved(self) -> bool:
        return self.status in {"answered", "approved", "denied", "expired", "failed"}


@sqlalchemy_materia.bless(deferred_models.DeferredActionBatch)
class DeferredActionBatch(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    conversation_id: uuid.UUID
    agent_tentacle_id: str
    run_name: str | None = "reception"
    status: DeferredBatchStatus = "pending"
    source_key: ConversationKey
    target_key: ConversationKey
    target_mode: ResponseTargetMode = "main"
    decision: TriageDecision | None = None
    requests: DeferredToolRequests
    actions: RelationCollection[DeferredAction] = Relationships()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None


DeferredAction.model_rebuild()
