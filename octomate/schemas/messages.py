from __future__ import annotations

import uuid
from dataclasses import field
from typing import Annotated, Any

from arcanus import Transmuter
from arcanus.base import Identity
from arcanus.dataclass import dataclass as arcanus_dataclass
from pydantic import ConfigDict, Discriminator, Field
from pydantic_ai.messages import ModelRequest as PydanticModelRequest
from pydantic_ai.messages import ModelResponse as PydanticModelResponse
from uuid_utils.compat import uuid7

from octomate.models.messages import ModelRequest as ModelRequestModel
from octomate.models.messages import ModelResponse as ModelResponseModel
from octomate.schemas.base import sqlalchemy_materia

# `metadata` is reserved on SQLAlchemy's DeclarativeBase, so the ORM column
# lives on the `meta` Python attribute. arcanus' bless resolves ORM attributes
# via `field_info.alias or field_name`, so the pydantic field keeps its
# user-facing `metadata` name and aliases to `meta` to bridge the two sides.
dataclass_config = ConfigDict(
    populate_by_name=True,
    validate_by_name=True,
    validate_by_alias=True,
)


@sqlalchemy_materia.bless(ModelRequestModel)
@arcanus_dataclass(config=dataclass_config)
class ModelRequest(Transmuter, PydanticModelRequest):
    id: Annotated[uuid.UUID, Identity] = field(default_factory=uuid7)
    session_id: uuid.UUID | None = None
    metadata: Annotated[dict[str, Any] | None, Field(alias="meta")] = None


@sqlalchemy_materia.bless(ModelResponseModel)
@arcanus_dataclass(config=dataclass_config)
class ModelResponse(Transmuter, PydanticModelResponse):
    id: Annotated[uuid.UUID, Identity] = field(default_factory=uuid7)
    session_id: uuid.UUID | None = None
    metadata: Annotated[dict[str, Any] | None, Field(alias="meta")] = None


ModelMessage = Annotated[ModelRequest | ModelResponse, Discriminator("kind")]
