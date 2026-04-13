from __future__ import annotations

from datetime import datetime
from typing import Annotated

from arcanus import BaseTransmuter, Relation, Relationship
from arcanus.base import Identity
from pydantic import ConfigDict, Field
from uuid_utils.compat import uuid7

from octomate.models.messages import Message as MessageModel
from octomate.transmuters.base import sqlalchemy_materia


@sqlalchemy_materia.bless(MessageModel)
class Message(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: Annotated[str, Identity] = Field(
        default_factory=lambda: str(uuid7()), frozen=True
    )
    message_id: str
    reply_id: str | None = None
    tentacle_id: str
    thread_id: str | None = None
    user: str
    chat: str
    agent_id: str | None = None
    timestamp: datetime = Field(default_factory=datetime.now)
    role: str
    content: str
    replied: Relation[Message] = Relationship(exclude=True)
