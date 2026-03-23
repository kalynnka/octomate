from __future__ import annotations

from datetime import datetime
from typing import Annotated, Optional

from arcanus import BaseTransmuter
from arcanus.base import Identity
from pydantic import Field

from octomate.models.messages import Message as MessageModel
from octomate.transmuters.base import sqlalchemy_materia


@sqlalchemy_materia.bless(MessageModel)
class Message(BaseTransmuter):
    id: Annotated[Optional[str], Identity] = Field(default=None, frozen=True)
    tentacle_id: str
    thread_id: str | None = None
    user: str
    chat: str
    agent_id: str | None = None
    timestamp: datetime = Field(default_factory=datetime.now)
    role: str
    content: str
