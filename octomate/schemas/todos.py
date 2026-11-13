"""Conversation-scoped todos and their write payload."""

from __future__ import annotations

from typing import Annotated, NotRequired, TypedDict

from arcanus import BaseTransmuter, Identity
from pydantic import UUID7, AwareDatetime, BaseModel, ConfigDict, Field
from pydantic_ai._utils import now_utc
from uuid_utils.compat import uuid7

from octomate.models import todos as todos_models
from octomate.schemas.base import sqlalchemy_materia
from octomate.types.todos import TodoStatus


@sqlalchemy_materia.bless(todos_models.Todo)
class Todo(BaseTransmuter):
    """One conversation-scoped todo, keyed by its short `ref`."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    conversation_id: UUID7
    ref: str
    content: str
    active_form: str = ""
    status: TodoStatus = "pending"
    position: int = 0
    parent_ref: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    created_at: AwareDatetime = Field(default_factory=now_utc)
    updated_at: AwareDatetime = Field(default_factory=now_utc)


class TodoWrite(TypedDict):
    """Write-payload for `TodoManager.write_todos` — one item to (re)create."""

    content: str
    status: NotRequired[TodoStatus]
    active_form: NotRequired[str]
    ref: NotRequired[str | None]
    parent_ref: NotRequired[str | None]
    depends_on: NotRequired[list[str]]


class TodoItem(BaseModel):
    """One todo in a `write_todos` call (the model rewrites the whole list)."""

    content: str = Field(
        description="Imperative task title, e.g. 'Fix the login bug'.",
    )
    status: TodoStatus = Field(
        default="pending",
        description="pending, in_progress, completed, or blocked.",
    )
    active_form: str = Field(
        description="Present-continuous label, e.g. 'Fixing the login bug'.",
    )
    id: str | None = Field(
        default=None,
        description="Existing todo id to keep; omit to create a new one.",
    )
    parent_id: str | None = Field(
        default=None,
        description="Parent todo id when this is a subtask.",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Ids of todos that must complete before this one.",
    )
