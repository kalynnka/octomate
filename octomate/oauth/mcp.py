"""MCP resource discovery and dynamic OAuth client registration."""

from __future__ import annotations

from datetime import UTC, datetime

from mcp.client.auth.oauth2 import check_registration_usable
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_client_info_from_metadata_url,
    create_client_registration_request,
    create_oauth_metadata_request,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
    get_client_metadata_scopes,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    handle_registration_response,
    should_use_client_metadata_url,
    validate_metadata_issuer,
)
from mcp.shared._httpx_utils import McpHttpClientFactory
from mcp.shared.auth import (
    OAuthClientMetadata,
)
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url
from pydantic import AnyHttpUrl, AnyUrl

from octomate.oauth.flows import HTTPS_URL, OAuthTokenExchange
from octomate.schemas.oauth import OAuthDiscoveryState
from octomate.types.oauth import HttpsUrl


class McpOAuthDiscovery:
    url: HttpsUrl
    httpx_client_factory: McpHttpClientFactory
    client_metadata_url: HttpsUrl | None
    discovery_state: OAuthDiscoveryState | None

    def __init__(
        self,
        *,
        url: HttpsUrl,
        httpx_client_factory: McpHttpClientFactory,
        client_metadata_url: HttpsUrl | None = None,
        discovery_state: OAuthDiscoveryState | None = None,
    ) -> None:
        self.url = url
        self.httpx_client_factory = httpx_client_factory
        self.client_metadata_url = client_metadata_url
        self.discovery_state = discovery_state

    async def resolve(self, callback_uri: AnyHttpUrl | None) -> OAuthTokenExchange:
        state = (
            await self.discover(callback_uri)
            if callback_uri is not None
            else self.discovery_state
        )
        if state is None:
            raise ValueError("MCP authorization has no stored client registration")
        if (
            state.metadata.authorization_endpoint is None
            or state.metadata.token_endpoint is None
        ):
            raise ValueError("MCP authorization has no authorization-code endpoints")
        return OAuthTokenExchange(
            authorization_endpoint=HTTPS_URL.validate_python(
                str(state.metadata.authorization_endpoint)
            ),
            token_endpoint=HTTPS_URL.validate_python(
                str(state.metadata.token_endpoint)
            ),
            client=state.client,
            scopes=state.scope.split() if state.scope else [],
            httpx_client_factory=self.httpx_client_factory,
            discovery_state=state,
        )

    async def discover(self, callback_uri: AnyHttpUrl) -> OAuthDiscoveryState:
        server_url = str(self.url)
        # Pydantic URL equality includes the class; SDK registrations use AnyUrl.
        redirect_uri = AnyUrl(str(callback_uri))
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
                    client, create_oauth_metadata_request(url)
                )
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                resource = await handle_protected_resource_response(response)
                if resource is not None:
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
                    client, create_oauth_metadata_request(url)
                )
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                ok, metadata = await handle_auth_metadata_response(response)
                if not ok:
                    break
                if metadata is not None:
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
                redirect_uris=[redirect_uri],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
                scope=scope,
            )
            if self.client_metadata_url is not None and should_use_client_metadata_url(
                metadata, str(self.client_metadata_url)
            ):
                info = create_client_info_from_metadata_url(
                    str(self.client_metadata_url), redirect_uris=[redirect_uri]
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
                info = await handle_registration_response(response)
            info.issuer = issuer
            check_registration_usable(info)
            if (
                info.client_secret_expires_at
                and info.client_secret_expires_at <= datetime.now(UTC).timestamp()
            ):
                raise ValueError("OAuth registration returned an expired client secret")
            info.validate_redirect_uri(redirect_uri)
        return OAuthDiscoveryState(
            resource=HTTPS_URL.validate_python(str(resource.resource)),
            metadata=metadata,
            client=info,
            scope=scope,
        )
