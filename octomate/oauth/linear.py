"""Linear's authorization-code OAuth: the upstream calls a connector composes.

The provider half of the Linear integration — the authorization request a browser
opens, the PKCE token exchange behind it, the refresh that keeps a day-long token
alive, and the viewer lookup that names the connection. It knows nothing about
agents, capabilities or MCP: application bootstrap composes this flow with a
callback transport into an `OAuthConnector`, registers it on the `OAuthManager`, and
hands that connector to `LinearCapability`.

Linear has no device grant, so this is the first flow that needs a browser to come
back somewhere — which is the whole reason the callback transports exist.
"""

from __future__ import annotations

from base64 import urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from secrets import token_urlsafe

import httpx
from pydantic import AnyHttpUrl, BaseModel, SecretStr, TypeAdapter

from octomate.config.integrations import LinearScope
from octomate.schemas.oauth import (
    AuthorizationCodeOAuthFlow,
    AuthorizationRequest,
    OAuthFlowContext,
    OAuthGrant,
)

LINEAR_AUTHORIZE_URL = "https://linear.app/oauth/authorize"
LINEAR_TOKEN_URL = "https://api.linear.app/oauth/token"
LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

# How long a user has to finish an authorization before the link stops working.
# Ours to choose rather than Linear's: the authorization request is stateless
# upstream, and this is the deadline on the operation holding its PKCE verifier.
AUTHORIZATION_LIFETIME = timedelta(minutes=10)


class LinearAccessTokenResponse(BaseModel):
    access_token: SecretStr
    refresh_token: SecretStr | None = None
    token_type: str = "Bearer"
    expires_in: int | None = None
    # Linear has answered with both a space-delimited string and a list across
    # versions of this endpoint; `granted_scopes` normalizes either.
    scope: str | list[str] = ""

    @property
    def granted_scopes(self) -> list[str]:
        if isinstance(self.scope, list):
            return self.scope
        return [scope for value in self.scope.split(" ") if (scope := value.strip())]


class LinearErrorResponse(BaseModel):
    error: str
    error_description: str = ""


class LinearViewer(BaseModel):
    id: str
    name: str


class LinearViewerData(BaseModel):
    viewer: LinearViewer


class LinearViewerResponse(BaseModel):
    data: LinearViewerData


# Annotated, not just assigned: `TypeAdapter(X | Y)` is handed a `UnionType` rather
# than a class, which no overload binds, so an unannotated adapter validates to
# `Unknown` and the branch below stops being checked at all.
LINEAR_TOKEN_RESPONSE_ADAPTER: TypeAdapter[
    LinearAccessTokenResponse | LinearErrorResponse
] = TypeAdapter(LinearAccessTokenResponse | LinearErrorResponse)


class LinearAuthorizationCodeOAuthFlow(AuthorizationCodeOAuthFlow):
    """Linear OAuth application authorization, token exchange and refresh."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: SecretStr | None,
        scopes: list[LinearScope],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes
        self.transport = transport

    async def start(
        self,
        context: OAuthFlowContext,
        callback_uri: AnyHttpUrl,
        state: SecretStr,
    ) -> AuthorizationRequest:
        # PKCE binds this authorization to the process that started it: the code a
        # callback carries is worth nothing without the verifier, which never leaves
        # Octomate. That matters more here than for a confidential client, because
        # the callback is a plain browser GET anyone could arrive at.
        verifier = token_urlsafe(64)
        challenge = (
            urlsafe_b64encode(sha256(verifier.encode()).digest()).decode().rstrip("=")
        )
        authorization_uri = httpx.URL(
            LINEAR_AUTHORIZE_URL,
            params={
                "client_id": self.client_id,
                "redirect_uri": str(callback_uri),
                "response_type": "code",
                # Linear takes its scope list comma-separated, not space-separated.
                "scope": ",".join(self.scopes),
                "state": state.get_secret_value(),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        return AuthorizationRequest(
            authorization_uri=AnyHttpUrl(str(authorization_uri)),
            code_verifier=SecretStr(verifier),
            expires_at=datetime.now(timezone.utc) + AUTHORIZATION_LIFETIME,
        )

    async def exchange(
        self,
        context: OAuthFlowContext,
        *,
        code: str,
        code_verifier: SecretStr | None,
        callback_uri: AnyHttpUrl,
    ) -> OAuthGrant:
        data = {
            "code": code,
            "redirect_uri": str(callback_uri),
            "client_id": self.client_id,
            "grant_type": "authorization_code",
        }
        if code_verifier is not None:
            data["code_verifier"] = code_verifier.get_secret_value()
        if self.client_secret is not None:
            data["client_secret"] = self.client_secret.get_secret_value()
        return await self.grant(data)

    async def refresh(self, refresh_token: SecretStr) -> OAuthGrant:
        data = {
            "refresh_token": refresh_token.get_secret_value(),
            "client_id": self.client_id,
            "grant_type": "refresh_token",
        }
        if self.client_secret is not None:
            data["client_secret"] = self.client_secret.get_secret_value()
        grant = await self.grant(data)
        if grant.refresh_token is not None:
            return grant
        # RFC 6749 lets an authorization server omit the refresh token when it has
        # not rotated one, which means keep the one just spent. Dropping it instead
        # would leave a connection that can never refresh again.
        return grant.model_copy(update={"refresh_token": refresh_token})

    async def grant(self, data: dict[str, str]) -> OAuthGrant:
        """Post to the token endpoint and name whoever the resulting token belongs to.

        Shared by the two ways of getting one. The viewer lookup rides along because
        a connection is stored under the account it authorizes, and a refresh writes
        the whole connection back — including which account it is for.
        """
        async with httpx.AsyncClient(transport=self.transport, timeout=20) as client:
            response = await client.post(
                LINEAR_TOKEN_URL,
                data=data,
                headers={"Accept": "application/json"},
            )
            if response.status_code >= 500:
                response.raise_for_status()
            token_response = LINEAR_TOKEN_RESPONSE_ADAPTER.validate_json(
                response.content
            )
            if isinstance(token_response, LinearErrorResponse):
                detail = token_response.error_description or token_response.error
                raise ValueError(f"Linear authorization failed: {detail}")

            viewer_response = await client.post(
                LINEAR_GRAPHQL_URL,
                json={"query": "{ viewer { id name } }"},
                headers={
                    "Authorization": (
                        f"Bearer {token_response.access_token.get_secret_value()}"
                    ),
                },
            )
            viewer_response.raise_for_status()
        viewer = LinearViewerResponse.model_validate_json(viewer_response.content).data
        return OAuthGrant(
            access_token=token_response.access_token,
            refresh_token=token_response.refresh_token,
            token_type=token_response.token_type,
            scopes=token_response.granted_scopes,
            subject=viewer.viewer.id,
            account_label=viewer.viewer.name,
            expires_at=(
                datetime.now(timezone.utc)
                + timedelta(seconds=token_response.expires_in)
                if token_response.expires_in is not None
                else None
            ),
        )
