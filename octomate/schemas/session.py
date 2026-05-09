from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import cached_property
from typing import Annotated, Literal

from arcanus import BaseTransmuter
from arcanus.base import Identity
from pydantic import BaseModel, ConfigDict, Field
from uuid_utils.compat import uuid7

from octomate.models.session import Session as SessionModel
from octomate.schemas.base import sqlalchemy_materia

ChatType = Literal["private", "group"]


@dataclass(frozen=True)
class SessionKey:
    channel_tentacle_id: str
    chat_type: ChatType
    chat_id: str
    user_id: str
    thread_id: str = ""

    @cached_property
    def is_group(self) -> bool:
        return self.chat_type == "group"

    @cached_property
    def group_id(self) -> str:
        return self.chat_id if self.chat_type == "group" else ""

    @cached_property
    def topic_memory_key(self) -> str:
        return f"topic:{self.channel_tentacle_id}:{self.chat_type}:{self.chat_id}:{self.thread_id or '-'}"

    @cached_property
    def user_memory_key(self) -> str:
        return f"user:{self.channel_tentacle_id}:{self.user_id}"

    def __str__(self) -> str:
        return (
            f"{self.channel_tentacle_id}/{self.chat_type}/{self.chat_id}"
            f"/{self.thread_id or '-'}/{self.user_id}"
        )


@sqlalchemy_materia.bless(SessionModel)
class Session(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)

    chat_type: ChatType
    chat_id: str
    thread_id: str = ""
    user_id: str

    channel_tentacle_id: str
    agent_tentacle_id: str | None = None

    name: str | None = None
    status: str = "active"

    @cached_property
    def key(self) -> SessionKey:
        return SessionKey(
            channel_tentacle_id=self.channel_tentacle_id,
            chat_type=self.chat_type,
            chat_id=self.chat_id,
            user_id=self.user_id,
            thread_id=self.thread_id,
        )


class UserProfile(BaseModel):
    model_config = ConfigDict(
        validate_by_alias=True,
        validate_by_name=True,
        extra="ignore",
        coerce_numbers_to_str=True,
        frozen=True,
    )

    user_id: str = "0"
    name: str = ""
    nickname: str | None = None
    gender: str | None = None
    age: int | None = None
    title: str | None = None
