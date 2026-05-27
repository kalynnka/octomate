from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from arcanus import BaseTransmuter, RelationCollection, Relationships
from arcanus.base import Identity
from pydantic import ConfigDict

from octomate.models.runs import AgentRun as AgentRunModel
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.messages import ModelRequest, ModelResponse


@sqlalchemy_materia.bless(AgentRunModel)
class AgentRun(BaseTransmuter):
    """One agent.run() invocation. PK is the pydantic-ai run_id (a uuid7 string)."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[str, Identity]
    conversation_id: uuid.UUID
    name: str | None = None
    started_at: datetime | None = None

    messages: RelationCollection[ModelRequest | ModelResponse] = Relationships()
