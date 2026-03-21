from __future__ import annotations

from typing import NamedTuple

from pydantic import BaseModel, ConfigDict


class SessionKey(NamedTuple):
    tentacle_id: str
    user_id: str
    group_id: str | None = None


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
