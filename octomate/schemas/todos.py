from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, NotRequired, TypedDict

from arcanus import BaseTransmuter
from arcanus.base import Identity
from pydantic import ConfigDict, Field
from uuid_utils.compat import uuid7

from octomate.models import todos as todos_models
from octomate.schemas.base import sqlalchemy_materia
from octomate.types.todos import TodoStatus


@sqlalchemy_materia.bless(todos_models.Todo)
class Todo(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    conversation_id: uuid.UUID
    ref: str
    content: str
    active_form: str = ""
    status: TodoStatus = "pending"
    position: int = 0
    parent_ref: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TodoWrite(TypedDict):
    """Write-payload for `TodoManager.write_todos` — one item to (re)create."""

    content: str
    status: NotRequired[TodoStatus]
    active_form: NotRequired[str]
    ref: NotRequired[str | None]
    parent_ref: NotRequired[str | None]
    depends_on: NotRequired[list[str]]
