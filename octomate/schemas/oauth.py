from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from base64 import b64decode
from dataclasses import dataclass
from datetime import datetime, timezone
from os import urandom
from typing import Annotated, ClassVar, Literal

from arcanus import BaseTransmuter
from arcanus.base import Identity
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
)
from uuid_utils.compat import uuid7

from octomate.models import oauth as oauth_models
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.user import User, UserProfile
from octomate.types.oauth import HttpsUrl, OAuthConnectionStatus


class OAuthCipher:
    """Encrypts short-lived operation secrets and durable OAuth tokens at rest."""

    def __init__(self, encryption_key: SecretStr) -> None:
        try:
            key = b64decode(
                encryption_key.get_secret_value(),
                altchars=b"-_",
                validate=True,
            )
        except ValueError as error:
            raise ValueError("oauth.encryption_key must be URL-safe base64") from error
        if len(key) != 32:
            raise ValueError("oauth.encryption_key must encode exactly 32 bytes")
        self.aes = AESGCM(key)

    def encrypt(self, value: str, *, context: str) -> bytes:
        nonce = urandom(12)
        return nonce + self.aes.encrypt(nonce, value.encode(), context.encode())

    def decrypt(self, value: bytes, *, context: str) -> str:
        nonce, ciphertext = value[:12], value[12:]
        return self.aes.decrypt(nonce, ciphertext, context.encode()).decode()


@dataclass(frozen=True)
class OAuthFlowContext:
    """The manager's owner-bound input to a connector strategy.

    ``OAuthManager`` creates it only after ``UserManager`` resolves the initiating
    channel profile to a registered user. The operation id correlates later device
    polling or browser callbacks; it never selects or overrides the owning user.
    """

    operation_id: uuid.UUID
    connector_id: str
    user: User
    profile: UserProfile


class DeviceAuthorizationResponse(BaseModel):
    """The authorization server's response when a device flow starts.

    A ``DeviceOAuthFlow`` returns this internal value to the manager. The manager
    adds its operation id before the verification instructions leave the OAuth
    boundary; the future completion step polls with server-side operation state.
    """

    verification_uri: HttpsUrl
    verification_uri_complete: HttpsUrl | None = None
    device_code: SecretStr = Field(exclude=True, repr=False)
    user_code: SecretStr = Field(repr=False)
    expires_at: datetime
    interval_seconds: int = Field(ge=1)


class AuthorizationRequest(BaseModel):
    """The provider-facing request that starts authorization-code OAuth.

    An ``AuthorizationCodeOAuthFlow`` builds this after receiving the callback URI.
    Its URI may contain OAuth state and must not be sent to a channel directly. The
    selected callback transport stages it and returns an ``AuthorizationLink``.
    """

    authorization_uri: AnyHttpUrl = Field(repr=False)
    expires_at: datetime


class DeviceAuthorization(BaseModel):
    """Device verification instructions returned by the manager.

    The channel presents the verification URI and user code to the initiating user.
    Its operation id lets the later polling and confirmation steps resume the exact
    owner-bound authorization without accepting a user id from the caller.
    """

    operation_id: uuid.UUID
    verification_uri: HttpsUrl
    verification_uri_complete: HttpsUrl | None = None
    user_code: SecretStr = Field(repr=False)
    expires_at: datetime
    interval_seconds: int = Field(ge=1)


class OAuthPending(BaseModel):
    """A device token exchange that the authorization server has not completed.

    The manager returns this when the user has not yet approved the device code.
    The channel can ask the same owner to confirm again after ``retry_after_seconds``.
    """

    retry_after_seconds: int = Field(ge=1)


class OAuthGrant(BaseModel):
    """Provider credentials returned after the user authorizes an operation.

    A device flow or authorization-code flow returns this only to ``OAuthManager``.
    The manager encrypts its secrets before persisting the owner-bound connection.
    """

    access_token: SecretStr = Field(repr=False)
    refresh_token: SecretStr | None = Field(default=None, repr=False)
    token_type: str = "bearer"
    scopes: list[str] = Field(default_factory=list)
    subject: str
    account_label: str
    expires_at: datetime | None = None


class AuthorizationLink(BaseModel):
    """The safe authorization-code link returned to the channel.

    The link contains only the operation UUID. Opening it makes the direct or relay
    transport retrieve the staged provider request, redirect the browser, and return
    the eventual callback to the manager's shared completion boundary.
    """

    operation_id: uuid.UUID
    authorization_uri: AnyHttpUrl
    expires_at: datetime


class DeviceOAuthFlow(ABC):
    """Starts the device branch after the manager has established its owner."""

    kind: ClassVar[Literal["device"]] = "device"

    @abstractmethod
    async def start(self, context: OAuthFlowContext) -> DeviceAuthorizationResponse:
        """Start a device authorization with the upstream authorization server."""

    @abstractmethod
    async def complete(
        self,
        context: OAuthFlowContext,
        device_code: SecretStr,
    ) -> OAuthGrant | OAuthPending:
        """Poll the provider and return either a completed grant or pending state."""


class AuthorizationCodeOAuthFlow(ABC):
    """Builds the provider request used by the browser authorization branch."""

    kind: ClassVar[Literal["authorization_code"]] = "authorization_code"

    @abstractmethod
    async def start(
        self,
        context: OAuthFlowContext,
        callback_uri: AnyHttpUrl,
    ) -> AuthorizationRequest:
        """Create the upstream authorization request for this operation."""


class OAuthCallbackTransport(ABC):
    """Moves authorization-code traffic across deployment ingress.

    After the manager establishes the owner, the transport supplies the provider's
    callback URI and stages the secret-bearing authorization request behind a
    UUID-only link. Direct HTTP and relay delivery converge on the same later manager
    completion step.
    """

    @property
    @abstractmethod
    def kind(self) -> Literal["direct_http", "relay"]:
        """The deployment transport selected by the connector."""

    @abstractmethod
    async def callback_uri(self, connector_id: str) -> AnyHttpUrl:
        """Return the provider-facing callback URI for this connector."""

    @abstractmethod
    async def prepare_authorization(
        self,
        context: OAuthFlowContext,
        authorization_uri: AnyHttpUrl,
    ) -> AnyHttpUrl:
        """Stage a provider URI and return the UUID-only link sent to the user."""


class DirectHttpOAuthCallbackTransport(OAuthCallbackTransport):
    """Authorization-code transport backed by Octomate's public HTTP routes."""

    @property
    def kind(self) -> Literal["direct_http"]:
        return "direct_http"


class RelayOAuthCallbackTransport(OAuthCallbackTransport):
    """Authorization-code transport backed by an external callback relay."""

    @property
    def kind(self) -> Literal["relay"]:
        return "relay"


OAuthStartResult = DeviceAuthorization | AuthorizationLink


class OAuthTokenPayload(BaseModel):
    """The plaintext token envelope immediately before encryption or after decryption."""

    access_token: SecretStr = Field(repr=False)
    refresh_token: SecretStr | None = Field(default=None, repr=False)
    token_type: str = "bearer"

    @field_serializer("access_token", "refresh_token", when_used="json")
    def serialize_secret(self, value: SecretStr | None) -> str | None:
        return value.get_secret_value() if value is not None else None


class DeviceOperationPayload(BaseModel):
    """The plaintext device-authorization envelope immediately before encryption.

    A provider mints a fresh code on every start, so an authorization that is still
    pending can only be shown to its user again out of what was sealed here when it
    began — nothing but the operation row survives the turn that started it.
    """

    device_code: SecretStr = Field(repr=False)
    user_code: SecretStr = Field(repr=False)
    verification_uri: HttpsUrl
    verification_uri_complete: HttpsUrl | None = None

    @field_serializer("device_code", "user_code", when_used="json")
    def serialize_secret(self, value: SecretStr) -> str:
        return value.get_secret_value()


@sqlalchemy_materia.bless(oauth_models.OAuthOperation)
class OAuthOperation(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    user_id: uuid.UUID
    profile_id: uuid.UUID
    connector_id: str
    encrypted_data: bytes = Field(repr=False)
    expires_at: datetime
    interval_seconds: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    consumed_at: datetime | None = None


@sqlalchemy_materia.bless(oauth_models.OAuthConnection)
class OAuthConnection(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    user_id: uuid.UUID
    connector_id: str
    status: OAuthConnectionStatus = "active"
    encrypted_tokens: bytes = Field(repr=False)
    subject: str
    account_label: str
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
