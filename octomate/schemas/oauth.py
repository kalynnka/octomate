from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal, TypeAlias

from arcanus import BaseTransmuter
from arcanus.base import Identity
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, TypeAdapter
from uuid_utils.compat import uuid7

from octomate.models import oauth as oauth_models
from octomate.schemas.base import sqlalchemy_materia
from octomate.types.oauth import OAuthConnectionKind, OAuthConnectionStatus

OAuthTokenDocument: TypeAlias = dict[str, JsonValue]
OAuthPrivateData: TypeAlias = dict[str, JsonValue]


@sqlalchemy_materia.bless(oauth_models.OAuthConnection)
class OAuthConnection(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    user_id: uuid.UUID
    key: str
    status: OAuthConnectionStatus = "active"
    subject: str | None = None
    account_label: str | None = None
    scopes: list[str] = Field(default_factory=list)
    encrypted_tokens: bytes = Field(exclude=True, repr=False)
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = 1


@sqlalchemy_materia.bless(oauth_models.ProviderOAuthConnection)
class ProviderOAuthConnection(OAuthConnection):
    kind: Literal["provider"] = "provider"
    provider: str


@sqlalchemy_materia.bless(oauth_models.McpOAuthConnection)
class McpOAuthConnection(OAuthConnection):
    kind: Literal["mcp"] = "mcp"
    resource_url: str
    authorization_server: str | None = None
    encrypted_client_information: bytes | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )


OAuthConnectionVariant: TypeAlias = Annotated[
    ProviderOAuthConnection | McpOAuthConnection,
    Field(discriminator="kind"),
]
OAuthConnectionAdapter = TypeAdapter(OAuthConnectionVariant)


@sqlalchemy_materia.bless(oauth_models.OAuthTransaction)
class OAuthTransaction(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    user_id: uuid.UUID
    profile_id: uuid.UUID
    kind: OAuthConnectionKind
    key: str
    replace_existing: bool = False
    ticket_hash: bytes = Field(exclude=True, repr=False)
    state_hash: bytes = Field(exclude=True, repr=False)
    encrypted_data: bytes = Field(exclude=True, repr=False)
    expires_at: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    callback_started_at: datetime | None = None
    consumed_at: datetime | None = None
    version: int = 1


class OAuthAuthorizationTicket(BaseModel):
    transaction_id: uuid.UUID
    ticket: SecretStr = Field(repr=False)
    expires_at: datetime


class OAuthTransactionSecrets(BaseModel):
    state: str = Field(repr=False)
    code_verifier: str = Field(repr=False)
    data: OAuthPrivateData = Field(default_factory=dict, repr=False)


class OAuthStartContext(BaseModel):
    transaction_id: uuid.UUID
    kind: OAuthConnectionKind
    key: str
    state: SecretStr = Field(repr=False)
    code_challenge: str
    data: OAuthPrivateData = Field(default_factory=dict, exclude=True, repr=False)


class OAuthCallbackContext(BaseModel):
    transaction_id: uuid.UUID
    kind: OAuthConnectionKind
    key: str
    code_verifier: SecretStr = Field(repr=False)
    data: OAuthPrivateData = Field(default_factory=dict, exclude=True, repr=False)


class ProviderOAuthCompletion(BaseModel):
    kind: Literal["provider"] = "provider"
    tokens: OAuthTokenDocument = Field(exclude=True, repr=False)
    subject: str | None = None
    account_label: str | None = None
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None


class McpOAuthCompletion(BaseModel):
    kind: Literal["mcp"] = "mcp"
    tokens: OAuthTokenDocument = Field(exclude=True, repr=False)
    authorization_server: str
    client_information: OAuthPrivateData | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    subject: str | None = None
    account_label: str | None = None
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None


OAuthCompletion: TypeAlias = Annotated[
    ProviderOAuthCompletion | McpOAuthCompletion,
    Field(discriminator="kind"),
]
OAuthCompletionAdapter = TypeAdapter(OAuthCompletion)


class OAuthConnectionSummary(BaseModel):
    id: uuid.UUID
    kind: OAuthConnectionKind
    key: str
    status: OAuthConnectionStatus
    subject: str | None
    account_label: str | None
    scopes: list[str]
    expires_at: datetime | None
    version: int
