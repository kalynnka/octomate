from __future__ import annotations

from base64 import urlsafe_b64encode
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pytest
from pydantic import AnyHttpUrl, SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import DiscordChannelConfig, OAuthConfig, OctomateConfig
from octomate.config.channels import DiscordOAuthClientConfig
from octomate.database import async_session
from octomate.oauth.flows import AuthorizationCodeFlow
from octomate.schemas.mcp import Mcp
from octomate.schemas.oauth import OAuthGrant
from octomate.schemas.user import UserProfile
from octomate.tentacles.discord import DiscordTentacle
from octomate.tentacles.discord.oauth import DiscordOAuthConnector, DiscordTokenExchange
from octomate.types.json import JsonObject
from tests.support.users import a_user, auth_config

CALLBACK_ORIGIN = AnyHttpUrl("http://127.0.0.1:5173")
CLIENT = DiscordOAuthClientConfig(
    client_id="12345", client_secret=SecretStr("app-secret")
)


def discord_host(transport: httpx2.AsyncBaseTransport) -> Octomate:
    host = Octomate(
        config=OctomateConfig(
            auth=auth_config(),
            oauth=OAuthConfig(
                callback_base_uri=CALLBACK_ORIGIN,
                authorization_lifetime=timedelta(minutes=3),
            ),
        ),
        oauth_encryption_key=SecretStr(urlsafe_b64encode(b"x" * 32).decode()),
    )
    host.oauth.httpx_client_factory = lambda headers=None, timeout=None, auth=None: (
        httpx2.AsyncClient(
            transport=transport, headers=headers, timeout=timeout, auth=auth
        )
    )
    host.connect(
        DiscordTentacle(
            id="discord-dev",
            octomate=host,
            config=DiscordChannelConfig(
                agents=["codex"], bot_token=SecretStr("bot-token"), oauth=CLIENT
            ),
        )
    )
    return host


@pytest.mark.parametrize("outcome", ["unlinked", "owned", "owned-by-other"])
async def test_profile_page_to_callback_links_the_verified_profile(
    in_memory_engine: AsyncEngine, outcome: str
) -> None:
    requests: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/api/oauth2/token":
            assert request.method == "POST"
            assert request.headers["Authorization"] == "Basic MTIzNDU6YXBwLXNlY3JldA=="
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["authorization_code"]
            assert form["code"] == ["discord-code"]
            assert form["redirect_uri"] == [
                "http://127.0.0.1:5173/oauth/discord-dev/callback"
            ]
            assert len(form["code_verifier"][0]) >= 43
            return httpx2.Response(
                200,
                json={
                    "access_token": "user-token",
                    "refresh_token": "refresh-token",
                    "token_type": "Bearer",
                    "scope": "identify",
                    "expires_in": 604800,
                },
            )
        assert request.url.path == "/api/v10/users/@me"
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer user-token"
        return httpx2.Response(
            200,
            json={
                "id": "987654321",
                "username": "alice",
                "global_name": "Alice",
            },
        )

    host = discord_host(httpx2.MockTransport(respond))
    user = await a_user("alice")
    owner = None
    if outcome.startswith("owned"):
        owner = user if outcome == "owned" else await a_user("someone-else")
        async with async_session() as db:
            db.add(
                UserProfile(
                    channel_tentacle_id="discord-dev",
                    channel_user_id="987654321",
                    user_id=owner.id,
                )
            )
            await db.commit()
    assert host.auth is not None
    password = SecretStr("Correct horse battery staple1!")
    user.password_hash = await host.auth.hash_password(password)
    async with async_session() as db:
        stored_user = await db.get(type(user), user.id)
        assert stored_user is not None
        stored_user.password_hash = user.password_hash
        await db.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host),
        base_url=str(CALLBACK_ORIGIN),
        headers={"X-Octomate-Request": "1"},
    ) as browser:
        refused = await browser.get("/api/auth/profile-authorizations")
        assert refused.status_code == 401
        refused_start = await browser.post(
            "/api/auth/profile-authorizations/discord-dev"
        )
        assert refused_start.status_code == 401
        login = await browser.post(
            "/api/auth/login",
            json={"username": user.username, "password": password.get_secret_value()},
        )
        assert login.status_code == 204
        available = await browser.get("/api/auth/profile-authorizations")
        assert available.json() == [{"id": "discord-dev", "type": "discord"}]
        no_csrf = await browser.post(
            "/api/auth/profile-authorizations/discord-dev",
            headers={"X-Octomate-Request": "0"},
        )
        assert no_csrf.status_code == 403
        unknown = await browser.post("/api/auth/profile-authorizations/missing")
        assert unknown.status_code == 404
        authorization = await browser.post(
            "/api/auth/profile-authorizations/discord-dev"
        )
        assert authorization.status_code == 200
        assert authorization.headers["cache-control"] == "no-store"
        assert "app-secret" not in authorization.text
        start = await browser.get(authorization.json()["authorization_uri"])
        assert start.status_code == 307
        provider_url = urlsplit(start.headers["location"])
        assert provider_url.netloc == "discord.com"
        assert provider_url.path == "/oauth2/authorize"
        params = parse_qs(provider_url.query)
        assert params["scope"] == ["identify"]
        assert params["client_id"] == [CLIENT.client_id]
        assert params["code_challenge_method"] == ["S256"]
        assert "app-secret" not in start.headers["location"]
        assert requests == []
        callback_params = {"state": params["state"][0], "code": "discord-code"}
        callback = await browser.get(
            "/oauth/discord-dev/callback", params=callback_params
        )
        assert callback.status_code == 200
        assert "location" not in callback.headers
        if outcome == "owned-by-other":
            assert "profile linking could not be completed" in callback.text
        else:
            assert "Your channel profile is linked" in callback.text
        replay = await browser.get(
            "/oauth/discord-dev/callback", params=callback_params
        )
        assert replay.status_code == 404
        assert await host.oauth.connection_status(user, "discord-dev") == "active"

    stored = await host.users.profile("discord-dev", "987654321")
    assert stored is not None
    assert stored.user_id == (owner.id if owner else user.id)
    assert stored.name == "Alice"
    assert stored.nickname == "alice"
    assert len(requests) == 2
    async with async_session() as db:
        assert await db.list(Mcp) == []
    assert host.mcp.available() == []


@pytest.mark.parametrize(
    "identity",
    [
        {"id": "987", "username": "bot", "bot": True},
        {"id": "0", "username": "alice"},
        {"id": "", "username": "alice"},
        {"id": "987", "username": ""},
    ],
)
async def test_identity_must_be_a_real_user(identity: JsonObject) -> None:
    host = discord_host(
        httpx2.MockTransport(lambda request: httpx2.Response(200, json=identity))
    )
    flow = host.oauth.connector("discord-dev").select_flow()
    assert isinstance(flow, AuthorizationCodeFlow)
    tokens = await flow.resolve_tokens()
    assert isinstance(tokens, DiscordTokenExchange)
    with pytest.raises(ValidationError):
        await tokens.grant(
            httpx2.Response(
                200,
                request=httpx2.Request("POST", "https://discord.com/api/oauth2/token"),
                json={
                    "access_token": "token",
                    "scope": "identify",
                    "token_type": "Bearer",
                },
            )
        )


async def test_unverified_grant_cannot_supply_a_profile() -> None:
    host = discord_host(httpx2.MockTransport(lambda request: httpx2.Response(500)))
    connector = host.oauth.connector("discord-dev")
    assert isinstance(connector, DiscordOAuthConnector)
    assert connector.mcp_url is None
    with pytest.raises(ValueError, match="verified Discord grant"):
        await connector.resolve_profile(
            OAuthGrant(access_token=SecretStr("token"), subject="987")
        )


def test_discord_oauth_requires_the_shared_callback_origin() -> None:
    host = Octomate(config=OctomateConfig(oauth=OAuthConfig(callback_base_uri=None)))
    with pytest.raises(ValueError, match=r"oauth\.callback_base_uri"):
        DiscordTentacle(
            id="discord",
            octomate=host,
            config=DiscordChannelConfig(
                agents=["codex"],
                bot_token=SecretStr("bot"),
                oauth=CLIENT,
            ),
        )


def test_discord_oauth_requires_encrypted_storage() -> None:
    with pytest.raises(
        ValidationError,
        match=r"oauth.encryption_key is required when tentacles.discord.oauth",
    ):
        OctomateConfig.model_validate(
            {
                "tentacles": {
                    "discord": {
                        "type": "discord",
                        "agents": ["codex"],
                        "bot_token": "bot",
                        "oauth": {"client_id": "12345", "client_secret": "secret"},
                    }
                },
                "oauth": {"encryption_key": None},
            }
        )
