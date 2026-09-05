from __future__ import annotations

import uuid
from base64 import urlsafe_b64encode
from hashlib import sha256
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import AnyHttpUrl, SecretStr

from octomate.oauth.linear import LinearAuthorizationCodeOAuthFlow
from octomate.schemas.oauth import OAuthFlowContext
from octomate.schemas.user import User, UserProfile

CALLBACK = AnyHttpUrl("http://127.0.0.1:8000/oauth/linear/callback")


def flow_context() -> OAuthFlowContext:
    user = User(username="alice", name="Alice")
    profile = UserProfile(
        channel_tentacle_id="lark",
        channel_user_id="OU1",
        user_id=user.id,
    )
    return OAuthFlowContext(
        operation_id=uuid.uuid4(),
        connector_id="linear",
        user=user,
        profile=profile,
    )


def token_transport(
    token: dict[str, object],
    *,
    on_token: object = None,
) -> httpx.MockTransport:
    """Answer the token endpoint with `token`, then name the viewer behind it."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            if callable(on_token):
                on_token(request)
            return httpx.Response(200, json=token)
        assert request.url.path == "/graphql"
        return httpx.Response(
            200, json={"data": {"viewer": {"id": "usr_42", "name": "Alice"}}}
        )

    return httpx.MockTransport(respond)


def linear_flow(
    transport: httpx.AsyncBaseTransport,
) -> LinearAuthorizationCodeOAuthFlow:
    return LinearAuthorizationCodeOAuthFlow(
        client_id="lin_client",
        client_secret=None,
        scopes=["read", "write"],
        transport=transport,
    )


async def test_start_builds_a_pkce_authorization_request() -> None:
    flow = linear_flow(httpx.MockTransport(lambda request: httpx.Response(500)))

    request = await flow.start(flow_context(), CALLBACK, SecretStr("op-id.random-half"))

    query = parse_qs(httpx.URL(str(request.authorization_uri)).query.decode())
    assert query["client_id"] == ["lin_client"]
    assert query["redirect_uri"] == [str(CALLBACK)]
    assert query["response_type"] == ["code"]
    # Linear takes its scopes comma-separated, unlike GitHub's spaces.
    assert query["scope"] == ["read,write"]
    assert query["state"] == ["op-id.random-half"]
    assert query["code_challenge_method"] == ["S256"]
    # The challenge is the verifier's digest, and only the verifier is kept back.
    assert request.code_verifier is not None
    verifier = request.code_verifier.get_secret_value()
    expected = (
        urlsafe_b64encode(sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    assert query["code_challenge"] == [expected]
    assert verifier not in str(request.authorization_uri)


async def test_exchange_spends_the_verifier_and_names_the_account() -> None:
    sent: list[httpx.Request] = []
    flow = linear_flow(
        token_transport(
            {
                "access_token": "linear-user-token",
                "refresh_token": "linear-refresh",
                "token_type": "Bearer",
                "expires_in": 86399,
                "scope": "read write",
            },
            on_token=sent.append,
        )
    )

    grant = await flow.exchange(
        flow_context(),
        code="auth-code",
        code_verifier=SecretStr("the-verifier"),
        callback_uri=CALLBACK,
    )

    form = parse_qs(sent[0].content.decode())
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["auth-code"]
    assert form["code_verifier"] == ["the-verifier"]
    # Replayed rather than rebuilt: the provider compares it with the one that
    # started the authorization.
    assert form["redirect_uri"] == [str(CALLBACK)]
    assert "client_secret" not in form
    assert grant.access_token.get_secret_value() == "linear-user-token"
    assert grant.scopes == ["read", "write"]
    assert grant.subject == "usr_42"
    assert grant.account_label == "Alice"
    assert grant.expires_at is not None


async def test_a_refused_exchange_is_a_value_error() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "code is spent"},
        )

    flow = linear_flow(httpx.MockTransport(respond))

    with pytest.raises(ValueError, match="code is spent"):
        await flow.exchange(
            flow_context(),
            code="auth-code",
            code_verifier=SecretStr("the-verifier"),
            callback_uri=CALLBACK,
        )


async def test_refresh_keeps_the_token_it_spent_when_none_comes_back() -> None:
    # RFC 6749 lets the server omit an unrotated refresh token, which means keep the
    # old one. Dropping it would leave a connection that can never refresh again.
    flow = linear_flow(
        token_transport(
            {
                "access_token": "linear-user-token-2",
                "token_type": "Bearer",
                "expires_in": 86399,
                "scope": ["read", "write"],
            }
        )
    )

    grant = await flow.refresh(SecretStr("linear-refresh"))

    assert grant.access_token.get_secret_value() == "linear-user-token-2"
    assert grant.refresh_token is not None
    assert grant.refresh_token.get_secret_value() == "linear-refresh"
    # A list-shaped `scope` normalizes the same way a space-delimited one does.
    assert grant.scopes == ["read", "write"]


async def test_refresh_takes_a_rotated_token_when_one_comes_back() -> None:
    flow = linear_flow(
        token_transport(
            {
                "access_token": "linear-user-token-2",
                "refresh_token": "linear-refresh-2",
                "token_type": "Bearer",
                "expires_in": 86399,
                "scope": "read write",
            }
        )
    )

    grant = await flow.refresh(SecretStr("linear-refresh"))

    assert grant.refresh_token is not None
    assert grant.refresh_token.get_secret_value() == "linear-refresh-2"
