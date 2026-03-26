from __future__ import annotations

from datetime import datetime
from typing import Annotated

from arcanus import BaseTransmuter
from arcanus.base import Identity
from pydantic import ConfigDict, Field
from uuid_utils.compat import uuid7

from octomate.models.threads import Thread as ThreadModel
from octomate.transmuters.base import sqlalchemy_materia


@sqlalchemy_materia.bless(ThreadModel)
class Thread(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: Annotated[str, Identity] = Field(
        default_factory=lambda: str(uuid7()), frozen=True
    )
    tentacle_id: str
    user_id: str
    group_id: str | None = None
    thread_id: str | None = None
    chat_id: str | None = None
    owner_tentacle: str
    created_at: datetime = Field(default_factory=datetime.now)
