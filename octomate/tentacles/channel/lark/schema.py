from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import Field, model_validator

from octomate.schemas.conversation import UserProfile


@dataclass(frozen=True)
class LarkOutboundMessage:
    msg_type: str
    content: str


class LarkUserProfile(UserProfile):
    user_id: str = Field(default="", validation_alias="open_id")
    title: str | None = Field(default=None, validation_alias="job_title")

    union_id: str = ""
    en_name: str = ""
    avatar_url: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: Any) -> Any:
        if isinstance(data, dict):
            avatar = data.pop("avatar", None)
            avatar_origin = None
            if isinstance(avatar, dict):
                avatar_origin = avatar.get("avatar_origin")
            elif avatar and hasattr(avatar, "avatar_origin"):
                avatar_origin = avatar.avatar_origin
            if avatar_origin:
                data.setdefault("avatar_url", avatar_origin)
            gender = data.get("gender")
            if gender:
                data["gender"] = {1: "male", 2: "female", 3: "other"}.get(gender)
            if not data.get("name"):
                data["name"] = data.get("nickname") or data.get("en_name") or ""
        return data
