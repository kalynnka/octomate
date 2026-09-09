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
    OAuthToken,
    ProtectedResourceMetadata,
)
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url
from pydantic import AnyHttpUrl, BaseModel, SecretStr, TypeAdapter

from octomate.schemas.oauth import (
    AuthorizationCodeOAuthFlow,
    AuthorizationRequest,
    McpOAuthState,
    OAuthFlowContext,
    OAuthGrant,
)
from octomate.types.oauth import HttpsUrl

HTTPS_URL: TypeAdapter[HttpsUrl] = TypeAdapter(HttpsUrl)


class OAuthRefreshRejected(ValueError):
    """The authorization server explicitly rejected the stored credentials."""


class OAuthTokenError(BaseModel):
    error: str


class McpOAuthFlow(AuthorizationCodeOAuthFlow):
    def __init__(
        self,
        *,
        url: HttpsUrl,
        httpx_client_factory: McpHttpClientFactory,
        client_metadata_url: HttpsUrl | None = None,
        state: McpOAuthState | None = None,
    ) -> None:
        self.url = url
        self.httpx_client_factory = httpx_client_factory
        self.client_metadata_url = client_metadata_url
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

    async def start(
        self,
        context: OAuthFlowContext,
        callback_uri: AnyHttpUrl,
        state: SecretStr,
    ) -> AuthorizationRequest:
        server_url = str(self.url)
        async with self.httpx_client_factory() as client:
            challenge = await self.send(
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
                response = await self.send(client, client.build_request("GET", url))
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
                response = await self.send(client, client.build_request("GET", url))
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
                response = await self.send(
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
        pkce = PKCEParameters.generate()
        params = {
            "response_type": "code",
            "client_id": info.client_id,
            "redirect_uri": str(callback_uri),
            "state": state.get_secret_value(),
            "code_challenge": pkce.code_challenge,
            "code_challenge_method": "S256",
            "resource": str(resource.resource),
        }
        if scope:
            params["scope"] = scope
            if "offline_access" in scope.split():
                params["prompt"] = "consent"
        return AuthorizationRequest(
            authorization_uri=AnyHttpUrl(
                str(
                    httpx2.URL(str(metadata.authorization_endpoint)).copy_merge_params(
                        params
                    )
                )
            ),
            code_verifier=SecretStr(pkce.code_verifier),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            mcp_oauth=McpOAuthState(
                resource=HTTPS_URL.validate_python(str(resource.resource)),
                metadata=metadata,
                client=info,
                scope=scope,
            ),
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
        return await self.grant(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier.get_secret_value(),
                "redirect_uri": str(callback_uri),
            }
        )

    async def refresh(self, refresh_token: SecretStr) -> OAuthGrant:
        grant = await self.grant(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token.get_secret_value(),
            }
        )
        if grant.refresh_token is None:
            grant.refresh_token = refresh_token
        return grant

    async def grant(self, data: dict[str, str]) -> OAuthGrant:
        state = self.state
        if state is None or state.metadata.token_endpoint is None:
            raise ValueError("MCP authorization has no stored client registration")
        data["resource"] = str(state.resource)
        info = state.client
        check_registration_usable(info)
        headers = {"Accept": "application/json"}
        match info.token_endpoint_auth_method:
            case "client_secret_basic":
                credentials = f"{quote_plus(info.client_id)}:{quote_plus(info.client_secret or '')}"
                headers["Authorization"] = (
                    "Basic " + b64encode(credentials.encode()).decode()
                )
            case "client_secret_post":
                data["client_id"] = info.client_id
                data["client_secret"] = info.client_secret or ""
            case "none" | None:
                data["client_id"] = info.client_id
            case _:
                raise ValueError(
                    "Unsupported OAuth token endpoint authentication method"
                )
        async with self.httpx_client_factory() as client:
            response = await self.send(
                client,
                client.build_request(
                    "POST",
                    str(state.metadata.token_endpoint),
                    data=data,
                    headers=headers,
                ),
            )
            if response.status_code in (400, 401):
                error = OAuthTokenError.model_validate_json(response.content)
                if error.error in ("invalid_grant", "invalid_client"):
                    raise OAuthRefreshRejected(
                        "OAuth credentials were rejected; reconnect this MCP"
                    )
            response.raise_for_status()
            token = OAuthToken.model_validate_json(response.content)
        scope = token.scope if token.scope is not None else state.scope
        return OAuthGrant(
            access_token=SecretStr(token.access_token),
            refresh_token=SecretStr(token.refresh_token)
            if token.refresh_token
            else None,
            token_type=token.token_type,
            scopes=scope.split() if scope else [],
            expires_at=datetime.now(UTC) + timedelta(seconds=token.expires_in)
            if token.expires_in is not None
            else None,
            mcp_oauth=state.model_copy(update={"scope": scope}),
        )
