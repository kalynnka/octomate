"""Slack's user-token OAuth flow, spoken to a stand-in for Slack: what the
authorization link carries, what the exchange posts, and who the grant names."""

from __future__ import annotations

import uuid
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pytest
from pydantic import AnyHttpUrl, SecretStr
from slack_sdk.web.async_slack_response import AsyncSlackResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import OAuthConfig, OctomateConfig, SlackChannelConfig
from octomate.config.channels import SlackOAuthClientConfig
from octomate.database import async_session
from octomate.oauth.flows import OAuthCodeFlow, OAuthRefreshRejected
from octomate.schemas.oauth import (
    AuthorizationLink,
    DirectHttpOAuthCallbackTransport,
    OAuthFlowContext,
    OAuthGrant,
)
from octomate.schemas.user import User, UserProfile
from octomate.tentacles.slack import SlackTentacle
from octomate.tentacles.slack.ink import SlackInk
from octomate.tentacles.slack.oauth import SlackOAuthConnector, SlackOAuthGrant
from octomate.tentacles.slack.schema import SlackUserProfile
from octomate.types.json import JsonObject
from tests.support.users import auth_config

CALLBACK = AnyHttpUrl("http://127.0.0.1:8000/oauth/slack/callback")


def flow_context() -> OAuthFlowContext:
    user = User(username="steve", name="Steve Li")
    profile = UserProfile(
        channel_tentacle_id="slack",
        channel_user_id="U1",
        user_id=user.id,
    )
    return OAuthFlowContext(
        operation_id=uuid.uuid4(),
        connector_id="slack",
        user=user,
        profile=profile,
    )


def slack_transport(
    token: JsonObject,
    *,
    posted: list[httpx2.Request] | None = None,
    identity: JsonObject | None = None,
) -> httpx2.MockTransport:
    """Answer the token endpoint with `token`, then `auth.test` for whoever holds
    the token it granted."""

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/oauth.v2.user.access":
            if posted is not None:
                posted.append(request)
            return httpx2.Response(200, json=token)
        assert request.url.path == "/api/auth.test"
        assert request.headers["Authorization"] == f"Bearer {token['access_token']}"
        return httpx2.Response(
            200,
            json=identity
            if identity is not None
            else {
                "ok": True,
                "url": "https://ancher.slack.com/",
                "team": "Ancher",
                "user": "steve.li",
                "team_id": "T1",
                "user_id": "U1",
            },
        )

    return httpx2.MockTransport(respond)


def slack_flow(
    transport: httpx2.AsyncBaseTransport, *, host: Octomate | None = None
) -> OAuthCodeFlow:
    host = host or Octomate(
        config=OctomateConfig(
            oauth=OAuthConfig(authorization_lifetime=timedelta(minutes=3))
        )
    )
    host.oauth.httpx_client_factory = lambda headers=None, timeout=None, auth=None: (
        httpx2.AsyncClient(
            transport=transport, headers=headers, timeout=timeout, auth=auth
        )
    )
    tentacle = SlackTentacle(
        id="slack",
        octomate=host,
        config=SlackChannelConfig(
            app_id="A-test",
            bot_token=SecretStr("xoxb-test"),
            app_token=SecretStr("xapp-test"),
            agents=["codex"],
            mcp=True,
            oauth=SlackOAuthClientConfig(
                client_id="1.2",
                client_secret=SecretStr("shh"),
                scopes=["search:read.public", "users:read"],
            ),
        ),
    )
    host.connect(tentacle)
    flow = host.oauth.connector("slack").select_flow()
    assert isinstance(host.oauth.connector("slack"), SlackOAuthConnector)
    assert isinstance(flow, OAuthCodeFlow)
    return flow


async def test_start_builds_a_pkce_authorization_request() -> None:
    flow = slack_flow(httpx2.MockTransport(lambda request: httpx2.Response(500)))

    before = datetime.now(UTC)
    request = await flow.start(flow_context(), CALLBACK, SecretStr("op-id.random-half"))
    assert (
        before + timedelta(minutes=3)
        <= request.expires_at
        <= datetime.now(UTC) + timedelta(minutes=3)
    )

    url = httpx2.URL(str(request.authorization_uri))
    assert f"{url.scheme}://{url.host}{url.path}" == (
        "https://slack.com/oauth/v2_user/authorize"
    )
    query = parse_qs(url.query.decode())
    assert query["client_id"] == ["1.2"]
    assert query["redirect_uri"] == [str(CALLBACK)]
    assert query["response_type"] == ["code"]
    # Space-delimited, as the standard MCP clients Slack points here spell it.
    assert query["scope"] == ["search:read.public users:read"]
    assert query["state"] == ["op-id.random-half"]
    assert query["code_challenge_method"] == ["S256"]
    assert request.code_verifier is not None
    verifier = request.code_verifier.get_secret_value()
    expected = (
        urlsafe_b64encode(sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    assert query["code_challenge"] == [expected]
    # Neither secret rides in the URL a browser opens.
    assert verifier not in str(request.authorization_uri)
    assert "shh" not in str(request.authorization_uri)


async def test_exchange_posts_the_secret_and_names_the_account() -> None:
    posted: list[httpx2.Request] = []
    flow = slack_flow(
        slack_transport(
            {
                "ok": True,
                "access_token": "xoxp-user",
                "token_type": "user",
                "authed_user": {"id": "U1", "scope": "search:read.public"},
                "team": {"id": "T1"},
            },
            posted=posted,
        )
    )

    grant = await flow.exchange(
        flow_context(),
        code="auth-code",
        code_verifier=SecretStr("pkce-verifier"),
        callback_uri=CALLBACK,
    )

    [request] = posted
    # `client_secret_post`, the one method Slack's metadata offers, with the PKCE
    # verifier alongside.
    assert parse_qs(request.content.decode()) == {
        "code": ["auth-code"],
        "redirect_uri": [str(CALLBACK)],
        "client_id": ["1.2"],
        "client_secret": ["shh"],
        "grant_type": ["authorization_code"],
        "code_verifier": ["pkce-verifier"],
    }
    assert grant.access_token.get_secret_value() == "xoxp-user"
    assert grant.refresh_token is None
    assert grant.expires_at is None
    assert grant.scopes == ["search:read.public"]
    # Named as Slack names the person: the id every other Slack surface uses.
    assert grant.subject == "U1"
    assert grant.account_label == "steve.li in Ancher"
    assert isinstance(grant, SlackOAuthGrant)
    assert grant.team_id == "T1"


async def test_a_rotating_token_keeps_its_refresh_token_and_expiry() -> None:
    flow = slack_flow(
        slack_transport(
            {
                "ok": True,
                "access_token": "xoxe.xoxp-user",
                "refresh_token": "xoxe-refresh",
                "expires_in": 43200,
                "token_type": "user",
            }
        )
    )

    grant = await flow.exchange(
        flow_context(),
        code="auth-code",
        code_verifier=SecretStr("pkce-verifier"),
        callback_uri=CALLBACK,
    )

    assert grant.refresh_token is not None
    assert grant.refresh_token.get_secret_value() == "xoxe-refresh"
    assert grant.expires_at is not None


async def test_refresh_keeps_the_token_it_spent_when_slack_returns_none() -> None:
    posted: list[httpx2.Request] = []
    flow = slack_flow(
        slack_transport(
            {"ok": True, "access_token": "xoxp-user-2", "token_type": "user"},
            posted=posted,
        )
    )

    grant = await flow.refresh(SecretStr("xoxe-refresh"))

    [request] = posted
    form = parse_qs(request.content.decode())
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == ["xoxe-refresh"]
    assert form["client_secret"] == ["shh"]
    assert grant.access_token.get_secret_value() == "xoxp-user-2"
    assert grant.refresh_token is not None
    assert grant.refresh_token.get_secret_value() == "xoxe-refresh"


async def test_slack_saying_no_is_a_refused_authorization() -> None:
    # Slack refuses with a 200 whose body says so, not with a status.
    flow = slack_flow(slack_transport({"ok": False, "error": "invalid_code"}))

    with pytest.raises(ValueError, match="OAuth token request failed: invalid_code"):
        await flow.exchange(
            flow_context(),
            code="stale-code",
            code_verifier=SecretStr("pkce-verifier"),
            callback_uri=CALLBACK,
        )


async def test_invalid_refresh_token_requires_reauthorization() -> None:
    flow = slack_flow(slack_transport({"ok": False, "error": "invalid_refresh_token"}))

    with pytest.raises(OAuthRefreshRejected):
        await flow.refresh(SecretStr("revoked-token"))


async def test_transient_token_failure_is_not_a_credential_rejection() -> None:
    flow = slack_flow(slack_transport({"ok": False, "error": "internal_error"}))

    with pytest.raises(ValueError, match="internal_error") as failure:
        await flow.refresh(SecretStr("valid-token"))

    assert not isinstance(failure.value, OAuthRefreshRejected)


@pytest.mark.parametrize(
    "identity",
    [
        {"ok": False, "error": "invalid_auth"},
        {"ok": True, "user": "steve", "user_id": "", "team": "Ancher", "team_id": "T1"},
        {"ok": True, "user": "steve", "user_id": "U1", "team": "Ancher", "team_id": ""},
        {
            "ok": True,
            "user": "bot",
            "user_id": "U1",
            "team": "Ancher",
            "team_id": "T1",
            "bot_id": "B1",
        },
    ],
)
async def test_slack_grant_requires_a_verified_human_and_workspace(
    identity: JsonObject,
) -> None:
    flow = slack_flow(
        slack_transport(
            {"ok": True, "access_token": "xoxp-user", "token_type": "user"},
            identity=identity,
        )
    )
    with pytest.raises(ValueError, match=r"invalid_auth|user_id|team_id|user token"):
        await flow.exchange(
            flow_context(),
            code="auth-code",
            code_verifier=SecretStr("pkce-verifier"),
            callback_uri=CALLBACK,
        )


@pytest.mark.parametrize("workspace", ["T1", "another-workspace"])
async def test_slack_profile_comes_from_the_grant_in_this_channels_workspace(
    workspace: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = slack_flow(
        slack_transport({"ok": True, "access_token": "xoxp-user", "token_type": "user"})
    )
    context = flow_context()
    assert context.profile is not None
    context.profile.channel_user_id = "initiating-chat-user"
    grant = await flow.exchange(
        context,
        code="auth-code",
        code_verifier=SecretStr("pkce-verifier"),
        callback_uri=CALLBACK,
    )
    ink = SlackInk(SecretStr("xoxb-test"))
    bot_identity = AsyncMock(
        return_value=AsyncSlackResponse(
            client=ink.client,
            http_verb="POST",
            api_url="https://slack.com/api/auth.test",
            req_args={},
            data={
                "ok": True,
                "user": "bot",
                "user_id": "UBOT",
                "team": "Ancher",
                "team_id": workspace,
                "bot_id": "B1",
            },
            headers={},
            status_code=200,
        )
    )
    monkeypatch.setattr(ink.client, "auth_test", bot_identity)
    fetch = AsyncMock(
        return_value=SlackUserProfile(
            channel_user_id="U1", name="Steve Li", nickname="steve", title="Engineer"
        )
    )
    monkeypatch.setattr(ink, "get_user_profile", fetch)
    connector = SlackOAuthConnector(
        id="slack-custom-alias",
        ink=ink,
        flows=[flow],
        callback_transport=DirectHttpOAuthCallbackTransport(
            AnyHttpUrl("https://octomate.example")
        ),
    )

    if workspace != "T1":
        with pytest.raises(ValueError, match="workspace"):
            await connector.resolve_profile(grant)
        fetch.assert_not_awaited()
    else:
        profile = await connector.resolve_profile(grant)
        assert profile.channel_user_id == "U1"
        assert profile.name == "Steve Li"
        assert profile.nickname == "steve"
        assert profile.title == "Engineer"
        assert profile.user_id is None
        fetch.assert_awaited_once_with("U1")
    bot_identity.assert_awaited_once_with()


async def test_slack_does_not_treat_a_generic_oauth_subject_as_a_verified_profile() -> (
    None
):
    connector = SlackOAuthConnector(
        id="slack",
        ink=SlackInk(SecretStr("xoxb-test")),
        flows=[slack_flow(httpx2.MockTransport(lambda request: httpx2.Response(500)))],
        callback_transport=DirectHttpOAuthCallbackTransport(
            AnyHttpUrl("https://octomate.example")
        ),
    )
    with pytest.raises(ValueError, match="verified Slack grant"):
        await connector.resolve_profile(
            OAuthGrant(access_token=SecretStr("not-slack"), subject="U1")
        )


@pytest.mark.parametrize("callback_session", ["original", "missing", "different"])
async def test_slack_callback_links_to_the_account_that_started_oauth(
    callback_session: str,
    in_memory_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = Octomate(
        config=OctomateConfig(
            auth=auth_config(),
            oauth=OAuthConfig(
                callback_base_uri=AnyHttpUrl("https://octomate.example"),
            ),
        ),
        oauth_encryption_key=SecretStr(urlsafe_b64encode(b"x" * 32).decode()),
    )
    slack_flow(
        slack_transport(
            {"ok": True, "access_token": "xoxp-user", "token_type": "user"}
        ),
        host=host,
    )
    connector = host.oauth.connector("slack")
    assert isinstance(connector, SlackOAuthConnector)
    monkeypatch.setattr(
        connector.ink.client,
        "auth_test",
        AsyncMock(
            return_value=AsyncSlackResponse(
                client=connector.ink.client,
                http_verb="POST",
                api_url="https://slack.com/api/auth.test",
                req_args={},
                headers={},
                status_code=200,
                data={
                    "ok": True,
                    "user": "bot",
                    "user_id": "UBOT",
                    "team": "Ancher",
                    "team_id": "T1",
                    "bot_id": "B1",
                },
            )
        ),
    )
    monkeypatch.setattr(
        connector.ink,
        "get_user_profile",
        AsyncMock(
            return_value=SlackUserProfile(
                channel_user_id="U1", name="Steve Li", nickname="steve"
            )
        ),
    )
    assert host.auth is not None
    password = SecretStr("Correct horse battery staple1!")
    user = User(username="steve", password_hash=await host.auth.hash_password(password))
    async with async_session() as session:
        session.add(user)
        await session.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host),
        base_url="https://octomate.example",
        headers={"X-Octomate-Request": "1"},
    ) as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": user.username, "password": password.get_secret_value()},
        )
        assert login.status_code == 204
        authorization_response = await client.post(
            "/api/auth/profile-authorizations/slack"
        )
        assert authorization_response.status_code == 200
        authorization = AuthorizationLink.model_validate(authorization_response.json())
        start = await client.get(str(authorization.authorization_uri))
        assert start.status_code == 307
        [state] = parse_qs(urlsplit(start.headers["location"]).query)["state"]
        if callback_session == "missing":
            client.cookies.clear()
        elif callback_session == "different":
            other = User(username="other", password_hash=user.password_hash)
            async with async_session() as session:
                session.add(other)
                await session.commit()
            switched = await client.post(
                "/api/auth/login",
                json={
                    "username": other.username,
                    "password": password.get_secret_value(),
                },
            )
            assert switched.status_code == 204
        callback = await client.get(
            "/oauth/slack/callback", params={"state": state, "code": "auth-code"}
        )
        assert callback.status_code == 200
        assert "Your channel profile is linked" in callback.text
        assert "location" not in callback.headers
        assert "xoxp-user" not in callback.text

    stored = await host.users.profile("slack", "U1")
    assert stored is not None
    assert stored.user_id == user.id
    assert stored.name == "Steve Li"
    token = await host.oauth.access_token(user, "slack")
    assert token is not None
    assert token.get_secret_value() == "xoxp-user"
