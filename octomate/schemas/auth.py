from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from arcanus import BaseTransmuter
from arcanus.base import Identity
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr
from uuid_utils.compat import uuid7

from octomate.models import auth as auth_models
from octomate.schemas.base import sqlalchemy_materia
from octomate.types.auth import ApiKeyScope


@sqlalchemy_materia.bless(auth_models.UserInvitation)
class UserInvitation(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    token_hash: SecretStr = Field(exclude=True, repr=False)
    expires_at: AwareDatetime
    consumed_at: AwareDatetime | None = None
    version: int = Field(default=1, exclude=True, repr=False)


@sqlalchemy_materia.bless(auth_models.UserSession)
class UserSession(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    user_id: uuid.UUID
    access_token_hash: SecretStr = Field(exclude=True, repr=False)
    refresh_token_hash: SecretStr = Field(exclude=True, repr=False)
    access_expires_at: AwareDatetime
    refresh_expires_at: AwareDatetime = Field(
        description="Absolute session deadline; refresh does not extend it."
    )
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    revoked_at: AwareDatetime | None = None
    version: int = Field(default=1, exclude=True, repr=False)


@sqlalchemy_materia.bless(auth_models.UserApiKey)
class UserApiKey(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    user_id: uuid.UUID
    name: str = Field(min_length=1, max_length=100)
    key_hash: SecretStr = Field(exclude=True, repr=False)
    key_prefix: str
    scopes: list[ApiKeyScope] = Field(min_length=1)
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: AwareDatetime | None = None
    revoked_at: AwareDatetime | None = None


class SessionTokens(BaseModel):
    """Secrets returned only by successful login or refresh."""

    session_id: uuid.UUID
    user_id: uuid.UUID
    access_token: SecretStr = Field(repr=False)
    refresh_token: SecretStr = Field(repr=False)
    access_expires_at: AwareDatetime
    refresh_expires_at: AwareDatetime


class IssuedApiKey(BaseModel):
    key: UserApiKey
    token: SecretStr = Field(repr=False)
