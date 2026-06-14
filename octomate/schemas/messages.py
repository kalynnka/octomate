from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from arcanus import BaseTransmuter, Transmuter
from arcanus.base import Identity
from arcanus.dataclass import dataclass as arcanus_dataclass
from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    model_validator,
)
from pydantic_ai._utils import now_utc
from pydantic_ai.messages import ModelRequest as PydanticModelRequest
from pydantic_ai.messages import ModelResponse as PydanticModelResponse
from pydantic_ai.messages import (
    TextContent,
    TextPart,
    ToolCallPart,
    UserContent,
    UserPromptPart,
)
from uuid_utils.compat import uuid7

from octomate.constants import SEND_TOOL_NAME
from octomate.models.messages import ModelMessage as ModelMessageModel
from octomate.models.messages import ModelRequest as ModelRequestModel
from octomate.models.messages import ModelResponse as ModelResponseModel
from octomate.schemas.base import sqlalchemy_materia
from octomate.types.json import JsonObject

# `metadata` is reserved on SQLAlchemy's DeclarativeBase, so the ORM column
# lives on the `meta` Python attribute. arcanus' bless resolves ORM attributes
# via `field_info.alias or field_name`, so the pydantic field keeps its
# user-facing `metadata` name and aliases to `meta` to bridge the two sides.
dataclass_config = ConfigDict(
    populate_by_name=True,
    validate_by_name=True,
    validate_by_alias=True,
)


def native_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _user_prompt_text(content: str | Sequence[UserContent]) -> str:
    """Plain text of a `UserPromptPart`: the str directly, or the text items of a
    content sequence (skipping multimodal parts and cache points)."""
    if isinstance(content, str):
        return content
    fragments: list[str] = []
    for item in content:
        if isinstance(item, str):
            fragments.append(item)
        elif isinstance(item, TextContent):
            fragments.append(item.content)
    return "\n".join(f for f in fragments if f)


@sqlalchemy_materia.bless(ModelMessageModel)
class ModelMessage(BaseTransmuter, ABC):
    """Polymorphic parent of the message transmuters — just the shared, queryable
    columns. Blessed to the abstract `model_messages` base so arcanus can run one
    polymorphic select that adapts each row to its `ModelRequest`/`ModelResponse`
    subtype (keyed on `kind`). Abstract and never instantiated; the subtypes can't
    subclass it (each already subclasses its pydantic-ai message type), so it is a
    sibling registration that the polymorphic result adapter discovers by `kind`."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    conversation_id: str | None = None
    role: Literal["user", "assistant"] = "assistant"
    message_text: str | None = None

    @abstractmethod
    def _concrete_message(self) -> None:
        """Marks `ModelMessage` abstract: it is a query anchor only. Every row
        materializes as a concrete sibling (`ModelRequest`/`ModelResponse`) via the
        polymorphic adapter, so the base itself is never instantiated."""


@sqlalchemy_materia.bless(ModelRequestModel)
@arcanus_dataclass(config=dataclass_config)
class ModelRequest(Transmuter, PydanticModelRequest):
    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    timestamp: Annotated[datetime, AfterValidator(native_utc)] | None = None
    metadata: Annotated[JsonObject | None, Field(alias="meta")] = None
    role: Literal["user", "assistant"] = "assistant"
    message_text: str | None = None

    @model_validator(mode="after")
    def _derive_message_fields(self) -> Self:
        # A request carrying a user prompt is the user's turn; one carrying only
        # tool returns is assistant-side. Text comes from the user prompt(s).
        self.role = (
            "user"
            if any(isinstance(part, UserPromptPart) for part in self.parts)
            else "assistant"
        )
        if self.message_text is None:
            text = "\n\n".join(
                t
                for part in self.parts
                if isinstance(part, UserPromptPart)
                and (t := _user_prompt_text(part.content))
            )
            self.message_text = text or None
        return self


@sqlalchemy_materia.bless(ModelResponseModel)
@arcanus_dataclass(config=dataclass_config)
class ModelResponse(Transmuter, PydanticModelResponse):
    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    timestamp: Annotated[datetime, AfterValidator(native_utc)] = Field(
        default_factory=now_utc
    )
    metadata: Annotated[JsonObject | None, Field(alias="meta")] = None
    role: Literal["user", "assistant"] = "assistant"
    message_text: str | None = None

    @model_validator(mode="after")
    def _derive_message_fields(self) -> Self:
        # A response is always assistant-side (the default). Its spoken text is the
        # answer parts plus anything delivered via send_message; thinking and other
        # tool calls are excluded.
        if self.message_text is None:
            fragments: list[str] = []
            for part in self.parts:
                if isinstance(part, TextPart):
                    if part.content:
                        fragments.append(part.content)
                elif (
                    isinstance(part, ToolCallPart) and part.tool_name == SEND_TOOL_NAME
                ):
                    fragments.append(str(part.args_as_dict()))
            self.message_text = "\n\n".join(fragments) or None
        return self
