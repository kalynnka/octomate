from __future__ import annotations

import uuid
from urllib.parse import parse_qs

import httpx
from pydantic import SecretStr

from octomate.oauth.github import GitHubDeviceOAuthFlow
from octomate.schemas.oauth import OAuthFlowContext, OAuthGrant, OAuthPending
from octomate.schemas.user import User, UserProfile


def flow_context() -> OAuthFlowContext:
    user = User(username="alice", name="Alice")
    profile = UserProfile(
        channel_tentacle_id="slack",
        channel_user_id="U1",
        user_id=user.id,
    )
    return OAuthFlowContext(
        operation_id=uuid.uuid4(),
        connector_id="github",
        user=user,
        profile=profile,
    )


async def test_start_requests_a_github_device_code() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/login/device/code"
        form = parse_qs(request.content.decode())
        assert form == {"client_id": ["Iv1.test"], "scope": ["repo read:org"]}
        return httpx.Response(
            200,
            json={
                "device_code": "device-secret",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": 5,
            },
        )

    flow = GitHubDeviceOAuthFlow(
        client_id="Iv1.test",
        scopes=["repo", "read:org"],
        transport=httpx.MockTransport(respond),
    )

    authorization = await flow.start(flow_context())

    assert authorization.device_code.get_secret_value() == "device-secret"
    assert authorization.user_code.get_secret_value() == "ABCD-EFGH"
    assert str(authorization.verification_uri) == "https://github.com/login/device"


async def test_complete_exchanges_the_code_and_identifies_the_account() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            form = parse_qs(request.content.decode())
            assert form["device_code"] == ["device-secret"]
            return httpx.Response(
                200,
                json={
                    "access_token": "github-user-token",
                    "token_type": "bearer",
                    "scope": "repo,read:org",
                },
            )
        assert request.url.path == "/user"
        assert request.headers["Authorization"] == "Bearer github-user-token"
        return httpx.Response(200, json={"id": 42, "login": "alice-gh"})

    flow = GitHubDeviceOAuthFlow(
        client_id="Iv1.test",
        scopes=["repo", "read:org"],
        transport=httpx.MockTransport(respond),
    )

    result = await flow.complete(flow_context(), SecretStr("device-secret"))

    assert isinstance(result, OAuthGrant)
    assert result.access_token.get_secret_value() == "github-user-token"
    assert result.subject == "42"
    assert result.account_label == "alice-gh"
    assert result.scopes == ["repo", "read:org"]


async def test_complete_reports_pending_without_fetching_the_account() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/login/oauth/access_token"
        return httpx.Response(
            200,
            json={
                "error": "authorization_pending",
                "error_description": "The authorization request is still pending.",
                "interval": 6,
            },
        )

    flow = GitHubDeviceOAuthFlow(
        client_id="Iv1.test",
        scopes=[],
        transport=httpx.MockTransport(respond),
    )

    result = await flow.complete(flow_context(), SecretStr("device-secret"))

    assert result == OAuthPending(retry_after_seconds=6)
