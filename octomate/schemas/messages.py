from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from arcanus import Transmuter
from arcanus.dataclass import dataclass as arcanus_dataclass
from pydantic import (
    AfterValidator,
    ConfigDict,
    Discriminator,
    Field,
    TypeAdapter,
)
from pydantic_ai._utils import now_utc
from pydantic_ai.messages import ModelRequest as PydanticModelRequest
from pydantic_ai.messages import ModelResponse as PydanticModelResponse

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


@sqlalchemy_materia.bless(ModelRequestModel)
@arcanus_dataclass(config=dataclass_config)
class ModelRequest(Transmuter, PydanticModelRequest):
    timestamp: Annotated[datetime, AfterValidator(native_utc)] | None = None
    metadata: Annotated[JsonObject | None, Field(alias="meta")] = None


@sqlalchemy_materia.bless(ModelResponseModel)
@arcanus_dataclass(config=dataclass_config)
class ModelResponse(Transmuter, PydanticModelResponse):
    timestamp: Annotated[datetime, AfterValidator(native_utc)] = Field(
        default_factory=now_utc
    )
    metadata: Annotated[JsonObject | None, Field(alias="meta")] = None


ModelMessage = Annotated[ModelRequest | ModelResponse, Discriminator("kind")]

ModelMessageListAdapter = TypeAdapter(list[ModelMessage])
