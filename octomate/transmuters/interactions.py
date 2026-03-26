from __future__ import annotations

from datetime import datetime
from typing import Annotated

from arcanus import BaseTransmuter
from arcanus.base import Identity
from pydantic import ConfigDict, Field
from uuid_utils.compat import uuid7

from octomate.models.interactions import Confirmation as ConfirmationModel
from octomate.models.interactions import Question as QuestionModel
from octomate.models.interactions import Todo as TodoModel
from octomate.transmuters.base import sqlalchemy_materia


@sqlalchemy_materia.bless(TodoModel)
class Todo(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: Annotated[str, Identity] = Field(
        default_factory=lambda: str(uuid7()), frozen=True
    )
    todo_id: str
    thread_id: str = ""
    title: str
    status: str = "pending"
    active_form: str | None = None
    assignee: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


@sqlalchemy_materia.bless(QuestionModel)
class Question(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: Annotated[str, Identity] = Field(
        default_factory=lambda: str(uuid7()), frozen=True
    )
    question_id: str
    thread_id: str
    text: str
    options: list[str] | None = None
    status: str = "pending"
    answer: str | None = None
    responder_id: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)


@sqlalchemy_materia.bless(ConfirmationModel)
class Confirmation(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: Annotated[str, Identity] = Field(
        default_factory=lambda: str(uuid7()), frozen=True
    )
    confirmation_id: str
    thread_id: str
    tool_name: str
    tool_call_id: str
    args: dict
    title: str = ""
    description: str = ""
    skill: str = ""
    approvers: list[str] = Field(default_factory=list)
    status: str = "pending"
    created_at: datetime = Field(default_factory=datetime.now)
    expires_at: datetime
