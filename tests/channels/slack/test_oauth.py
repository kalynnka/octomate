"""Slack's user-token OAuth flow, spoken to a stand-in for Slack: what the
authorization link carries, what the exchange posts, and who the grant names."""

from __future__ import annotations

import uuid
from base64 import urlsafe_b64encode
from hashlib import sha256
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import AnyHttpUrl, SecretStr

from octomate.schemas.oauth import OAuthFlowContext
from octomate.schemas.user import User, UserProfile
from octomate.tentacles.slack.oauth import SlackAuthorizationCodeOAuthFlow

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
    token: dict[str, object],
    *,
    posted: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """Answer the token endpoint with `token`, then `auth.test` for whoever holds
    the token it granted."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/oauth.v2.user.access":
            if posted is not None:
                posted.append(request)
            return httpx.Response(200, json=token)
        assert request.url.path == "/api/auth.test"
        assert request.headers["Authorization"] == f"Bearer {token['access_token']}"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "url": "https://ancher.slack.com/",
                "team": "Ancher",
                "user": "steve.li",
                "team_id": "T1",
                "user_id": "U1",
            },
        )

    return httpx.MockTransport(respond)


def slack_flow(transport: httpx.AsyncBaseTransport) -> SlackAuthorizationCodeOAuthFlow:
    return SlackAuthorizationCodeOAuthFlow(
        client_id="1.2",
        client_secret=SecretStr("shh"),
        scopes=["search:read.public", "users:read"],
        transport=transport,
    )


async def test_start_builds_a_pkce_authorization_request() -> None:
    flow = slack_flow(httpx.MockTransport(lambda request: httpx.Response(500)))

    request = await flow.start(flow_context(), CALLBACK, SecretStr("op-id.random-half"))

    url = httpx.URL(str(request.authorization_uri))
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
    posted: list[httpx.Request] = []
    flow = slack_flow(
        slack_transport(
            {
                "ok": True,
                "access_token": "xoxp-user",
                "token_type": "user",
                "authed_user": {"id": "U1", "scope": "search:read.public,users:read"},
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
    assert grant.scopes == ["search:read.public", "users:read"]
    # Named as Slack names the person: the id every other Slack surface uses.
    assert grant.subject == "U1"
    assert grant.account_label == "steve.li in Ancher"


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
        flow_context(), code="auth-code", code_verifier=None, callback_uri=CALLBACK
    )

    assert grant.refresh_token is not None
    assert grant.refresh_token.get_secret_value() == "xoxe-refresh"
    assert grant.expires_at is not None


async def test_refresh_keeps_the_token_it_spent_when_slack_returns_none() -> None:
    posted: list[httpx.Request] = []
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

    with pytest.raises(ValueError, match="Slack authorization failed: invalid_code"):
        await flow.exchange(
            flow_context(),
            code="stale-code",
            code_verifier=None,
            callback_uri=CALLBACK,
        )
