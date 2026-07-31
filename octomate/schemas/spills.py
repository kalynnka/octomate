from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from arcanus import BaseTransmuter
from arcanus.base import Identity
from pydantic import ConfigDict, Field
from uuid_utils.compat import uuid7

from octomate.models import spills as spills_models
from octomate.schemas.base import sqlalchemy_materia


@sqlalchemy_materia.bless(spills_models.ToolOutputSpill)
class ToolOutputSpill(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    handle: str
    payload: bytes
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
