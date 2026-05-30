from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from pydantic import Field, model_validator
from typing_extensions import NotRequired, TypedDict

from octomate.schemas.conversation import UserProfile
from octomate.schemas.deferred import DeferredQuestion

NonEmptyStr = Annotated[str, Field(min_length=1)]


class LarkApprovalActionValue(TypedDict):
    action: NonEmptyStr
    batch_id: UUID
    action_id: UUID
    tool_name: NotRequired[str]


class LarkQuestionActionValue(TypedDict):
    action: NonEmptyStr
    batch_id: UUID
    questions: Annotated[list[DeferredQuestion], Field(min_length=1)]
    page: int
    answers: dict[UUID, str]


class LarkQuestionFormValue(TypedDict, total=False):
    answer: str | None
    choice: str | None


@dataclass(frozen=True)
class LarkOutboundMessage:
    msg_type: str
    content: str


@dataclass(frozen=True)
class LarkStreamCard:
    card_id: str
    element_id: str


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
