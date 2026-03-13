from __future__ import annotations

from typing import NamedTuple

from pydantic import BaseModel


class SessionKey(NamedTuple):
    tentacle_id: str
    user_id: int | str
    group_id: int | str | None = None


class Sender(BaseModel):
    user_id: int | str = 0
    nickname: str = ""
    card: str | None = None
    role: str | None = None
    sex: str | None = None
    age: int | None = None
    area: str | None = None
    level: str | None = None
    title: str | None = None


class Anonymous(BaseModel):
    id: int
    name: str
    flag: str
