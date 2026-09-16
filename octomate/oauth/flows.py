"""Configured OAuth flows shared by channels and MCP integrations."""

from __future__ import annotations

from base64 import b64encode
from datetime import UTC, datetime, timedelta
from urllib.parse import quote_plus

import httpx2
from mcp.client.auth.oauth2 import PKCEParameters, check_registration_usable
from mcp.shared._httpx_utils import McpHttpClientFactory
from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, TypeAdapter

from octomate.schemas.oauth import (
    AuthorizationCodeOAuthFlow,
    AuthorizationRequest,
    DeviceAuthorizationResponse,
    DeviceOAuthFlow,
    McpOAuthState,
    OAuthFlowContext,
    OAuthGrant,
    OAuthPending,
)
from octomate.types.oauth import HttpsUrl

HTTPS_URL: TypeAdapter[HttpsUrl] = TypeAdapter(HttpsUrl)


class OAuthRefreshRejected(ValueError):
    """The authorization server explicitly rejected the stored credentials."""


class DeviceCodeResponse(BaseModel):
    device_code: SecretStr
    user_code: SecretStr
    verification_uri: HttpsUrl
    verification_uri_complete: HttpsUrl | None = None
    expires_in: int = Field(gt=0)
    interval: int = Field(default=5, ge=1)


class TokenResponse(BaseModel):
    access_token: SecretStr
    token_type: str = "bearer"
    refresh_token: SecretStr | None = None
    expires_in: int | None = Field(default=None, ge=0)
    scope: str | None = None


class TokenError(BaseModel):
    error: str
    interval: int | None = Field(default=None, ge=1)


class OAuthTokenExchange:
    token_endpoint: HttpsUrl
    client_id: str
    client_secret: SecretStr | None
    token_endpoint_auth_method: str
    scopes: list[str]
    scope_separator: str
    invalid_credentials_errors: list[str]
    httpx_client_factory: McpHttpClientFactory
    state: McpOAuthState | None

    def __init__(
        self,
        *,
        token_endpoint: HttpsUrl,
        client_id: str,
        client_secret: SecretStr | None,
        token_endpoint_auth_method: str,
        scopes: list[str],
        scope_separator: str = " ",
        invalid_credentials_errors: list[str] | None = None,
        httpx_client_factory: McpHttpClientFactory,
        state: McpOAuthState | None = None,
    ) -> None:
        self.token_endpoint = token_endpoint
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_endpoint_auth_method = token_endpoint_auth_method
        self.scopes = scopes
        self.scope_separator = scope_separator
        self.invalid_credentials_errors = (
            invalid_credentials_errors
            if invalid_credentials_errors is not None
            else ["invalid_grant", "invalid_client"]
        )
        self.httpx_client_factory = httpx_client_factory
        self.state = state

    @staticmethod
    async def send(
        client: httpx2.AsyncClient, request: httpx2.Request
    ) -> httpx2.Response:
        url = HTTPS_URL.validate_python(str(request.url))
        if url.username or url.password or url.fragment:
            raise ValueError(
                "OAuth endpoints cannot contain URL credentials or fragments"
            )
        response = await client.send(request, follow_redirects=False)
        if response.is_redirect:
            raise ValueError("OAuth endpoint redirects are not supported")
        return response

    async def request(self, url: HttpsUrl, data: dict[str, str]) -> httpx2.Response:
        if self.state is not None:
            check_registration_usable(self.state.client)
            data["resource"] = str(self.state.resource)
        headers = {"Accept": "application/json"}
        match self.token_endpoint_auth_method:
            case "client_secret_basic":
                if self.client_secret is None:
                    raise ValueError("OAuth client secret is required")
                credentials = (
                    f"{quote_plus(self.client_id)}:"
                    f"{quote_plus(self.client_secret.get_secret_value())}"
                )
                headers["Authorization"] = (
                    "Basic " + b64encode(credentials.encode()).decode()
                )
            case "client_secret_post":
                if self.client_secret is None:
                    raise ValueError("OAuth client secret is required")
                data["client_id"] = self.client_id
                data["client_secret"] = self.client_secret.get_secret_value()
            case "none":
                data["client_id"] = self.client_id
            case _:
                raise ValueError(
                    "Unsupported OAuth token endpoint authentication method"
                )
        async with self.httpx_client_factory() as client:
            return await self.send(
                client,
                client.build_request("POST", str(url), data=data, headers=headers),
            )

    async def grant(self, response: httpx2.Response) -> OAuthGrant:
        if response.status_code not in (200, 400, 401):
            response.raise_for_status()
        # Some OAuth servers return protocol errors with HTTP 200.
        if "error" in response.json():
            error = TokenError.model_validate_json(response.content)
            if error.error in self.invalid_credentials_errors:
                raise OAuthRefreshRejected(
                    "OAuth credentials were rejected; reconnect this provider"
                )
            response.raise_for_status()
            raise ValueError(f"OAuth token request failed: {error.error}")
        response.raise_for_status()
        token = TokenResponse.model_validate_json(response.content)
        scopes = (
            [
                scope.strip()
                for scope in token.scope.split(self.scope_separator)
                if scope.strip()
            ]
            if token.scope is not None
            else self.scopes
        )
        return OAuthGrant(
            access_token=token.access_token,
            token_type=token.token_type,
            refresh_token=token.refresh_token,
            expires_at=datetime.now(UTC) + timedelta(seconds=token.expires_in)
            if token.expires_in is not None
            else None,
            scopes=scopes,
            mcp_oauth=self.state.model_copy(update={"scope": " ".join(scopes)})
            if self.state is not None
            else None,
        )

    async def refresh(self, refresh_token: SecretStr) -> OAuthGrant:
        response = await self.request(
            self.token_endpoint,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token.get_secret_value(),
            },
        )
        grant = await self.grant(response)
        if grant.refresh_token is None:
            grant.refresh_token = refresh_token
        return grant


class OAuthCodeFlow(AuthorizationCodeOAuthFlow):
    """Authorization-code OAuth with explicitly configured endpoints and credentials."""

    authorization_endpoint: HttpsUrl
    authorization_lifetime: timedelta
    tokens: OAuthTokenExchange

    def __init__(
        self,
        *,
        authorization_endpoint: HttpsUrl,
        authorization_lifetime: timedelta,
        tokens: OAuthTokenExchange,
    ) -> None:
        self.authorization_endpoint = authorization_endpoint
        self.authorization_lifetime = authorization_lifetime
        self.tokens = tokens

    async def start(
        self,
        context: OAuthFlowContext,
        callback_uri: AnyHttpUrl,
        state: SecretStr,
    ) -> AuthorizationRequest:
        pkce = PKCEParameters.generate()
        params = {
            "response_type": "code",
            "client_id": self.tokens.client_id,
            "redirect_uri": str(callback_uri),
            "state": state.get_secret_value(),
            "code_challenge": pkce.code_challenge,
            "code_challenge_method": "S256",
        }
        if self.tokens.scopes:
            params["scope"] = " ".join(self.tokens.scopes)
        return AuthorizationRequest(
            authorization_uri=AnyHttpUrl(
                str(
                    httpx2.URL(str(self.authorization_endpoint)).copy_merge_params(
                        params
                    )
                )
            ),
            code_verifier=SecretStr(pkce.code_verifier),
            expires_at=datetime.now(UTC) + self.authorization_lifetime,
        )

    async def exchange(
        self,
        context: OAuthFlowContext,
        *,
        code: str,
        code_verifier: SecretStr | None,
        callback_uri: AnyHttpUrl,
    ) -> OAuthGrant:
        if code_verifier is None:
            raise ValueError("OAuth requires a PKCE verifier")
        response = await self.tokens.request(
            self.tokens.token_endpoint,
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier.get_secret_value(),
                "redirect_uri": str(callback_uri),
            },
        )
        return await self.tokens.grant(response)

    async def refresh(self, refresh_token: SecretStr) -> OAuthGrant:
        return await self.tokens.refresh(refresh_token)


class OAuthDeviceFlow(DeviceOAuthFlow):
    device_authorization_endpoint: HttpsUrl
    tokens: OAuthTokenExchange

    def __init__(
        self,
        *,
        device_authorization_endpoint: HttpsUrl,
        tokens: OAuthTokenExchange,
    ) -> None:
        self.device_authorization_endpoint = device_authorization_endpoint
        self.tokens = tokens

    async def start(self, context: OAuthFlowContext) -> DeviceAuthorizationResponse:
        data = {"scope": " ".join(self.tokens.scopes)} if self.tokens.scopes else {}
        response = await self.tokens.request(self.device_authorization_endpoint, data)
        response.raise_for_status()
        authorization = DeviceCodeResponse.model_validate_json(response.content)
        return DeviceAuthorizationResponse(
            verification_uri=authorization.verification_uri,
            verification_uri_complete=authorization.verification_uri_complete,
            device_code=authorization.device_code,
            user_code=authorization.user_code,
            expires_at=datetime.now(UTC) + timedelta(seconds=authorization.expires_in),
            interval_seconds=authorization.interval,
        )

    async def complete(
        self, context: OAuthFlowContext, device_code: SecretStr
    ) -> OAuthGrant | OAuthPending:
        response = await self.tokens.request(
            self.tokens.token_endpoint,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code.get_secret_value(),
            },
        )
        if "error" in response.json():
            error = TokenError.model_validate_json(response.content)
            interval = max(context.interval_seconds or 5, error.interval or 5)
            match error.error:
                case "authorization_pending":
                    return OAuthPending(retry_after_seconds=interval)
                case "slow_down":
                    return OAuthPending(retry_after_seconds=interval + 5)
        return await self.tokens.grant(response)

    async def refresh(self, refresh_token: SecretStr) -> OAuthGrant:
        return await self.tokens.refresh(refresh_token)
