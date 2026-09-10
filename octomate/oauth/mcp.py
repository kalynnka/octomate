from __future__ import annotations

from base64 import b64encode
from datetime import UTC, datetime, timedelta
from urllib.parse import quote_plus

import httpx2
from mcp.client.auth.oauth2 import PKCEParameters, check_registration_usable
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_client_info_from_metadata_url,
    create_client_registration_request,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
    get_client_metadata_scopes,
    should_use_client_metadata_url,
    validate_metadata_issuer,
)
from mcp.shared._httpx_utils import McpHttpClientFactory
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    ProtectedResourceMetadata,
)
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url
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

    def grant(self, response: httpx2.Response) -> OAuthGrant:
        if response.status_code not in (200, 400, 401):
            response.raise_for_status()
        # Some OAuth servers return protocol errors with HTTP 200.
        if "error" in response.json():
            error = TokenError.model_validate_json(response.content)
            if error.error in self.invalid_credentials_errors:
                raise OAuthRefreshRejected(
                    "OAuth credentials were rejected; reconnect this MCP"
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
        grant = self.grant(response)
        if grant.refresh_token is None:
            grant.refresh_token = refresh_token
        return grant


class McpOAuthFlow(AuthorizationCodeOAuthFlow):
    url: HttpsUrl
    httpx_client_factory: McpHttpClientFactory
    client_metadata_url: HttpsUrl | None
    authorization_endpoint: HttpsUrl | None
    tokens: OAuthTokenExchange | None

    def __init__(
        self,
        *,
        url: HttpsUrl,
        httpx_client_factory: McpHttpClientFactory,
        client_metadata_url: HttpsUrl | None = None,
        state: McpOAuthState | None = None,
        authorization_endpoint: HttpsUrl | None = None,
        tokens: OAuthTokenExchange | None = None,
    ) -> None:
        self.url = url
        self.httpx_client_factory = httpx_client_factory
        self.client_metadata_url = client_metadata_url
        self.authorization_endpoint = authorization_endpoint
        if state is not None:
            if state.metadata.token_endpoint is None:
                raise ValueError("MCP authorization has no token endpoint")
            tokens = OAuthTokenExchange(
                token_endpoint=HTTPS_URL.validate_python(
                    str(state.metadata.token_endpoint)
                ),
                client_id=state.client.client_id,
                client_secret=SecretStr(state.client.client_secret)
                if state.client.client_secret is not None
                else None,
                token_endpoint_auth_method=state.client.token_endpoint_auth_method
                or "none",
                scopes=state.scope.split() if state.scope else [],
                httpx_client_factory=httpx_client_factory,
                state=state,
            )
        self.tokens = tokens

    async def discover(self, callback_uri: AnyHttpUrl) -> McpOAuthState:
        server_url = str(self.url)
        async with self.httpx_client_factory() as client:
            challenge = await OAuthTokenExchange.send(
                client,
                client.build_request(
                    "POST",
                    server_url,
                    headers={
                        "MCP-Protocol-Version": "2026-07-28",
                        "Accept": "application/json, text/event-stream",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": "authorize",
                        "method": "tools/list",
                        "params": {},
                    },
                ),
            )
            if challenge.status_code != 401:
                challenge.raise_for_status()
                raise ValueError("MCP endpoint did not request OAuth authorization")
            resource = None
            for url in build_protected_resource_metadata_discovery_urls(
                extract_resource_metadata_from_www_auth(challenge), server_url
            ):
                response = await OAuthTokenExchange.send(
                    client, client.build_request("GET", url)
                )
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                resource = ProtectedResourceMetadata.model_validate_json(
                    response.content
                )
                break
            if resource is None or not resource.authorization_servers:
                raise ValueError("MCP endpoint has no OAuth resource metadata")
            if not check_resource_allowed(
                requested_resource=resource_url_from_server_url(server_url),
                configured_resource=str(resource.resource),
            ):
                raise ValueError("OAuth resource does not match the installed MCP")
            issuer = str(resource.authorization_servers[0])
            metadata = None
            for url in build_oauth_authorization_server_metadata_discovery_urls(
                issuer, server_url
            ):
                response = await OAuthTokenExchange.send(
                    client, client.build_request("GET", url)
                )
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                metadata = OAuthMetadata.model_validate_json(response.content)
                validate_metadata_issuer(metadata, issuer)
                break
            if (
                metadata is None
                or metadata.authorization_endpoint is None
                or metadata.token_endpoint is None
            ):
                raise ValueError("OAuth server has no authorization-code endpoints")
            HTTPS_URL.validate_python(str(metadata.authorization_endpoint))
            HTTPS_URL.validate_python(str(metadata.token_endpoint))
            if "S256" not in (metadata.code_challenge_methods_supported or []):
                raise ValueError("OAuth server must support PKCE S256")
            scope = get_client_metadata_scopes(
                extract_scope_from_www_auth(challenge),
                resource,
                metadata,
                ["authorization_code", "refresh_token"],
            )
            registration = OAuthClientMetadata(
                client_name="Octomate",
                redirect_uris=[callback_uri],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
                scope=scope,
            )
            if self.client_metadata_url is not None and should_use_client_metadata_url(
                metadata, str(self.client_metadata_url)
            ):
                info = create_client_info_from_metadata_url(
                    str(self.client_metadata_url), redirect_uris=[callback_uri]
                )
            else:
                if metadata.registration_endpoint is None:
                    raise ValueError(
                        "This MCP requires pre-registered app configuration through a McpTentacle"
                    )
                response = await OAuthTokenExchange.send(
                    client,
                    create_client_registration_request(metadata, registration, issuer),
                )
                response.raise_for_status()
                info = OAuthClientInformationFull.model_validate_json(response.content)
            info.issuer = issuer
            check_registration_usable(info)
            if (
                info.client_secret_expires_at
                and info.client_secret_expires_at <= datetime.now(UTC).timestamp()
            ):
                raise ValueError("OAuth registration returned an expired client secret")
            if str(callback_uri) not in [str(uri) for uri in info.redirect_uris or []]:
                raise ValueError("OAuth registration did not accept the callback URI")
        return McpOAuthState(
            resource=HTTPS_URL.validate_python(str(resource.resource)),
            metadata=metadata,
            client=info,
            scope=scope,
        )

    async def start(
        self,
        context: OAuthFlowContext,
        callback_uri: AnyHttpUrl,
        state: SecretStr,
    ) -> AuthorizationRequest:
        mcp_oauth = None
        if self.authorization_endpoint is None:
            mcp_oauth = await self.discover(callback_uri)
            authorization_endpoint = mcp_oauth.metadata.authorization_endpoint
            if authorization_endpoint is None:
                raise ValueError("MCP authorization has no authorization endpoint")
            client_id = mcp_oauth.client.client_id
            scope = mcp_oauth.scope
        else:
            authorization_endpoint = self.authorization_endpoint
            if self.tokens is None:
                raise ValueError("Configured OAuth requires client credentials")
            client_id = self.tokens.client_id
            scope = " ".join(self.tokens.scopes)
        pkce = PKCEParameters.generate()
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": str(callback_uri),
            "state": state.get_secret_value(),
            "code_challenge": pkce.code_challenge,
            "code_challenge_method": "S256",
        }
        if mcp_oauth is not None:
            params["resource"] = str(mcp_oauth.resource)
        if scope:
            params["scope"] = scope
            if mcp_oauth is not None and "offline_access" in scope.split():
                params["prompt"] = "consent"
        return AuthorizationRequest(
            authorization_uri=AnyHttpUrl(
                str(httpx2.URL(str(authorization_endpoint)).copy_merge_params(params))
            ),
            code_verifier=SecretStr(pkce.code_verifier),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            mcp_oauth=mcp_oauth,
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
            raise ValueError("MCP OAuth requires a PKCE verifier")
        if self.tokens is None:
            raise ValueError("MCP authorization has no stored client registration")
        response = await self.tokens.request(
            self.tokens.token_endpoint,
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier.get_secret_value(),
                "redirect_uri": str(callback_uri),
            },
        )
        return self.tokens.grant(response)

    async def refresh(self, refresh_token: SecretStr) -> OAuthGrant:
        if self.tokens is None:
            raise ValueError("MCP authorization has no stored client registration")
        return await self.tokens.refresh(refresh_token)


class McpDeviceOAuthFlow(DeviceOAuthFlow):
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
        return self.tokens.grant(response)

    async def refresh(self, refresh_token: SecretStr) -> OAuthGrant:
        return await self.tokens.refresh(refresh_token)
