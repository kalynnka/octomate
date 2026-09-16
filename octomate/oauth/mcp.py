"""MCP resource discovery and dynamic OAuth client registration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx2
from mcp.client.auth.oauth2 import check_registration_usable
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
from pydantic import AnyHttpUrl, SecretStr

from octomate.oauth.flows import HTTPS_URL, OAuthCodeFlow, OAuthTokenExchange
from octomate.schemas.oauth import (
    AuthorizationCodeOAuthFlow,
    AuthorizationRequest,
    McpOAuthState,
    OAuthFlowContext,
    OAuthGrant,
)
from octomate.types.oauth import HttpsUrl


class McpOAuthFlow(AuthorizationCodeOAuthFlow):
    url: HttpsUrl
    authorization_lifetime: timedelta
    httpx_client_factory: McpHttpClientFactory
    client_metadata_url: HttpsUrl | None
    state: McpOAuthState | None

    def __init__(
        self,
        *,
        url: HttpsUrl,
        httpx_client_factory: McpHttpClientFactory,
        authorization_lifetime: timedelta,
        client_metadata_url: HttpsUrl | None = None,
        state: McpOAuthState | None = None,
    ) -> None:
        self.url = url
        self.authorization_lifetime = authorization_lifetime
        self.httpx_client_factory = httpx_client_factory
        self.client_metadata_url = client_metadata_url
        self.state = state

    def configured(self, state: McpOAuthState) -> OAuthCodeFlow:
        if (
            state.metadata.authorization_endpoint is None
            or state.metadata.token_endpoint is None
        ):
            raise ValueError("MCP authorization has no authorization-code endpoints")
        return OAuthCodeFlow(
            authorization_endpoint=HTTPS_URL.validate_python(
                str(state.metadata.authorization_endpoint)
            ),
            authorization_lifetime=self.authorization_lifetime,
            tokens=OAuthTokenExchange(
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
                httpx_client_factory=self.httpx_client_factory,
                state=state,
            ),
        )

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
        mcp_oauth = await self.discover(callback_uri)
        authorization = await self.configured(mcp_oauth).start(
            context, callback_uri, state
        )
        params = {"resource": str(mcp_oauth.resource)}
        if mcp_oauth.scope and "offline_access" in mcp_oauth.scope.split():
            params["prompt"] = "consent"
        authorization.authorization_uri = AnyHttpUrl(
            str(
                httpx2.URL(str(authorization.authorization_uri)).copy_merge_params(
                    params
                )
            )
        )
        authorization.mcp_oauth = mcp_oauth
        return authorization

    async def exchange(
        self,
        context: OAuthFlowContext,
        *,
        code: str,
        code_verifier: SecretStr | None,
        callback_uri: AnyHttpUrl,
    ) -> OAuthGrant:
        if self.state is None:
            raise ValueError("MCP authorization has no stored client registration")
        return await self.configured(self.state).exchange(
            context, code=code, code_verifier=code_verifier, callback_uri=callback_uri
        )

    async def refresh(self, refresh_token: SecretStr) -> OAuthGrant:
        if self.state is None:
            raise ValueError("MCP authorization has no stored client registration")
        return await self.configured(self.state).refresh(refresh_token)
