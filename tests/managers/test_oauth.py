from __future__ import annotations

import asyncio
import contextlib
import inspect
from base64 import urlsafe_b64encode
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import cast

import httpx
import pytest
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import AnyHttpUrl, SecretStr, TypeAdapter
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import ToolReturn
from pydantic_ai.toolsets import (
    DeferredLoadingToolset,
    FunctionToolset,
    PrefixedToolset,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.capabilities.github import (
    GITHUB_OAUTH_INSTRUCTION,
    GITHUB_RETIRED_INSTRUCTION,
    GitHubCapability,
)
from octomate.capabilities.harness.events import OAuthAuthorizationEvent
from octomate.config.integrations import GITHUB_CONNECTOR_ID
from octomate.config.users import UserConfig
from octomate.database import async_session
from octomate.oauth.base import McpConnectionAuth
from octomate.managers.oauth import OAuthConnector, OAuthManager
from octomate.managers.user import UserManager
from octomate.schemas.oauth import (
    AuthorizationCodeOAuthFlow,
    AuthorizationLink,
    AuthorizationRequest,
    DeviceAuthorization,
    DeviceAuthorizationResponse,
    DeviceOAuthFlow,
    DirectHttpOAuthCallbackTransport,
    OAuthConnection,
    OAuthFlowContext,
    OAuthGrant,
    OAuthOperation,
    OAuthPending,
    RelayOAuthCallbackTransport,
)
from octomate.schemas.user import UserProfile
from octomate.types.oauth import HttpsUrl

URL_ADAPTER = TypeAdapter(AnyHttpUrl)
HTTPS_URL_ADAPTER = TypeAdapter(HttpsUrl)
ENCRYPTION_KEY = SecretStr(urlsafe_b64encode(bytes(range(32))).decode())


@pytest.fixture(autouse=True)
async def database(in_memory_engine: AsyncEngine) -> AsyncIterator[None]:
    yield


class FakeDeviceFlow(DeviceOAuthFlow):
    def __init__(self) -> None:
        self.context: OAuthFlowContext | None = None
        self.starts = 0
        # How long the codes this flow hands out stay good; negative to mint one
        # that is already past its deadline.
        self.lifetime = timedelta(minutes=15)
        self.completion: OAuthGrant | OAuthPending = OAuthGrant(
            access_token=SecretStr("github-token"),
            subject="42",
            account_label="luhui",
            scopes=["repo"],
        )

    async def start(self, context: OAuthFlowContext) -> DeviceAuthorizationResponse:
        self.context = context
        self.starts += 1
        return DeviceAuthorizationResponse(
            verification_uri=HTTPS_URL_ADAPTER.validate_python(
                "https://github.com/login/device"
            ),
            device_code=SecretStr("device-secret"),
            user_code=SecretStr("ABCD-EFGH"),
            expires_at=datetime.now(timezone.utc) + self.lifetime,
            interval_seconds=5,
        )

    async def complete(
        self,
        context: OAuthFlowContext,
        device_code: SecretStr,
    ) -> OAuthGrant | OAuthPending:
        self.context = context
        assert device_code.get_secret_value() == "device-secret"
        return self.completion


class FakeAuthorizationCodeFlow(AuthorizationCodeOAuthFlow):
    def __init__(self) -> None:
        self.context: OAuthFlowContext | None = None
        self.callback: AnyHttpUrl | None = None

    async def start(
        self,
        context: OAuthFlowContext,
        callback_uri: AnyHttpUrl,
    ) -> AuthorizationRequest:
        self.context = context
        self.callback = callback_uri
        return AuthorizationRequest(
            authorization_uri=URL_ADAPTER.validate_python(
                "https://example.com/authorize?state=provider-secret-state"
            ),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )


class FakeDirectHttpTransport(DirectHttpOAuthCallbackTransport):
    def __init__(self) -> None:
        self.staged: dict[str, AnyHttpUrl] = {}

    async def callback_uri(self, connector_id: str) -> AnyHttpUrl:
        return URL_ADAPTER.validate_python(
            f"https://octomate.example/oauth/callback/{connector_id}"
        )

    async def prepare_authorization(
        self,
        context: OAuthFlowContext,
        authorization_uri: AnyHttpUrl,
    ) -> AnyHttpUrl:
        self.staged[str(context.operation_id)] = authorization_uri
        return URL_ADAPTER.validate_python(
            f"https://octomate.example/oauth/start/{context.operation_id}"
        )


class FakeRelayTransport(RelayOAuthCallbackTransport):
    async def callback_uri(self, connector_id: str) -> AnyHttpUrl:
        return URL_ADAPTER.validate_python(
            f"https://relay.example/oauth/callback/{connector_id}"
        )

    async def prepare_authorization(
        self,
        context: OAuthFlowContext,
        authorization_uri: AnyHttpUrl,
    ) -> AnyHttpUrl:
        return URL_ADAPTER.validate_python(
            f"https://relay.example/oauth/start/{context.operation_id}"
        )


async def linked_user_manager() -> tuple[UserManager, UserProfile]:
    users = UserManager(
        {
            "luhui": UserConfig.model_validate(
                {"profiles": {"slack": {"channel_user_id": "U1"}}}
            )
        }
    )
    await users.reconcile()
    async with async_session() as session:
        profile = await session.one_or_none(
            UserProfile,
            expressions=[UserProfile["channel_user_id"] == "U1"],
        )
    assert profile is not None
    return users, profile


def _mcp_auth(capability: GitHubCapability) -> McpConnectionAuth:
    """The credential object inside a bound capability's MCP session."""
    toolset = capability.toolset
    assert isinstance(toolset, DeferredLoadingToolset)
    prefixed = toolset.wrapped
    assert isinstance(prefixed, PrefixedToolset)
    inner = prefixed.wrapped
    assert isinstance(inner, MCPToolset)
    transport = inner.client.transport
    assert isinstance(transport, StreamableHttpTransport)
    auth = transport.auth
    assert isinstance(auth, McpConnectionAuth)
    return auth


async def _drive_auth(auth: McpConnectionAuth, *, status: int) -> httpx.Request:
    """Run one request/response round of the auth flow and return what it sent."""
    flow = auth.async_auth_flow(httpx.Request("POST", "https://mcp.example/mcp"))
    request = await anext(flow)
    with contextlib.suppress(StopAsyncIteration):
        await flow.asend(httpx.Response(status, request=request))
    return request


def test_connector_requires_the_transport_appropriate_to_its_flow() -> None:
    with pytest.raises(ValueError, match="does not use"):
        OAuthConnector(
            id="github",
            flow=FakeDeviceFlow(),
            callback_transport=FakeDirectHttpTransport(),
        )

    with pytest.raises(ValueError, match="requires a callback"):
        OAuthConnector(id="linear", flow=FakeAuthorizationCodeFlow())


def test_manager_rejects_duplicate_connector_ids() -> None:
    manager = OAuthManager(users=UserManager(), encryption_key=ENCRYPTION_KEY)
    manager.register(OAuthConnector(id="github", flow=FakeDeviceFlow()))

    with pytest.raises(ValueError, match="already registered"):
        manager.register(OAuthConnector(id="github", flow=FakeDeviceFlow()))


async def test_device_flow_uses_the_registered_channel_owner() -> None:
    users, profile = await linked_user_manager()
    flow = FakeDeviceFlow()
    manager = OAuthManager(
        users=users,
        encryption_key=ENCRYPTION_KEY,
        connectors=[OAuthConnector(id="github", flow=flow)],
    )

    authorization = await manager.start(profile, "github")

    assert isinstance(authorization, DeviceAuthorization)
    assert flow.context is not None
    assert flow.context.operation_id == authorization.operation_id
    assert flow.context.user.username == "luhui"
    assert flow.context.profile.id == profile.id


async def test_visitor_cannot_start_oauth() -> None:
    users = UserManager()
    visitor = await users.ensure_profile(
        "slack",
        UserProfile(channel_user_id="visitor", name="Visitor"),
    )
    flow = FakeDeviceFlow()
    manager = OAuthManager(
        users=users,
        encryption_key=ENCRYPTION_KEY,
        connectors=[OAuthConnector(id="github", flow=flow)],
    )

    with pytest.raises(ValueError, match="registered user"):
        await manager.start(visitor, "github")

    assert flow.context is None


async def test_authorization_code_flow_uses_the_selected_transport() -> None:
    users, profile = await linked_user_manager()
    flow = FakeAuthorizationCodeFlow()
    transport = FakeDirectHttpTransport()
    manager = OAuthManager(
        users=users,
        connectors=[
            OAuthConnector(
                id="linear",
                flow=flow,
                callback_transport=transport,
            )
        ],
    )

    authorization = await manager.start(profile, "linear")

    assert isinstance(authorization, AuthorizationLink)
    assert flow.callback == URL_ADAPTER.validate_python(
        "https://octomate.example/oauth/callback/linear"
    )
    assert authorization.authorization_uri == URL_ADAPTER.validate_python(
        f"https://octomate.example/oauth/start/{authorization.operation_id}"
    )
    staged = transport.staged[str(authorization.operation_id)]
    assert "provider-secret-state" in str(staged)
    assert "provider-secret-state" not in str(authorization.authorization_uri)


async def test_authorization_code_connector_can_select_a_relay() -> None:
    users, profile = await linked_user_manager()
    manager = OAuthManager(
        users=users,
        connectors=[
            OAuthConnector(
                id="linear",
                flow=FakeAuthorizationCodeFlow(),
                callback_transport=FakeRelayTransport(),
            )
        ],
    )

    authorization = await manager.start(profile, "linear")

    assert isinstance(authorization, AuthorizationLink)
    assert str(authorization.authorization_uri).startswith(
        "https://relay.example/oauth/start/"
    )


async def test_device_completion_persists_an_owner_bound_encrypted_token() -> None:
    users, profile = await linked_user_manager()
    flow = FakeDeviceFlow()
    manager = OAuthManager(
        users=users,
        encryption_key=ENCRYPTION_KEY,
        connectors=[OAuthConnector(id="github", flow=flow)],
    )
    await manager.start(profile, "github")
    async with async_session() as session:
        [operation] = await session.list(OAuthOperation, limit=None)
    assert b"device-secret" not in operation.encrypted_data

    grant = await manager.complete_latest(profile, "github")

    assert isinstance(grant, OAuthGrant)
    token = await manager.access_token(profile, "github")
    assert token is not None
    assert token.get_secret_value() == "github-token"
    async with async_session() as session:
        [connection] = await session.list(OAuthConnection, limit=None)
    assert b"github-token" not in connection.encrypted_tokens


async def test_pending_device_completion_keeps_the_operation_available() -> None:
    users, profile = await linked_user_manager()
    flow = FakeDeviceFlow()
    flow.completion = OAuthPending(retry_after_seconds=7)
    manager = OAuthManager(
        users=users,
        encryption_key=ENCRYPTION_KEY,
        connectors=[OAuthConnector(id="github", flow=flow)],
    )
    await manager.start(profile, "github")

    first = await manager.complete_latest(profile, "github")
    second = await manager.complete_latest(profile, "github")

    assert first == OAuthPending(retry_after_seconds=7)
    assert second == OAuthPending(retry_after_seconds=7)


async def test_complete_latest_orders_uuid7_operation_ids() -> None:
    users, profile = await linked_user_manager()
    flow = FakeDeviceFlow()
    manager = OAuthManager(
        users=users,
        encryption_key=ENCRYPTION_KEY,
        connectors=[OAuthConnector(id="github", flow=flow)],
    )
    # The first authorization has to be past its deadline, or starting again would
    # resume it instead of leaving two operations to order.
    flow.lifetime = timedelta(seconds=-1)
    first = await manager.start(profile, "github")
    await asyncio.sleep(0.002)
    flow.lifetime = timedelta(minutes=15)
    second = await manager.start(profile, "github")
    assert second.operation_id.int > first.operation_id.int

    await manager.complete_latest(profile, "github")

    assert flow.context is not None
    assert flow.context.operation_id == second.operation_id


async def test_start_resumes_a_device_authorization_that_is_still_live() -> None:
    users, profile = await linked_user_manager()
    flow = FakeDeviceFlow()
    manager = OAuthManager(
        users=users,
        encryption_key=ENCRYPTION_KEY,
        connectors=[OAuthConnector(id="github", flow=flow)],
    )

    first = await manager.start(profile, "github")
    second = await manager.start(profile, "github")

    # Asking again hands back the code the user is already looking at; a second
    # trip upstream would mint a new one and strand the first.
    assert flow.starts == 1
    assert second == first


async def test_start_replaces_a_device_authorization_that_has_expired() -> None:
    users, profile = await linked_user_manager()
    flow = FakeDeviceFlow()
    flow.lifetime = timedelta(seconds=-1)
    manager = OAuthManager(
        users=users,
        encryption_key=ENCRYPTION_KEY,
        connectors=[OAuthConnector(id="github", flow=flow)],
    )

    first = await manager.start(profile, "github")
    flow.lifetime = timedelta(minutes=15)
    second = await manager.start(profile, "github")

    assert flow.starts == 2
    assert second.operation_id != first.operation_id


async def test_device_operation_can_only_be_confirmed_by_its_starting_profile() -> None:
    users = UserManager(
        {
            "luhui": UserConfig.model_validate(
                {
                    "profiles": {
                        "slack": {"channel_user_id": "U1"},
                        "lark": {"channel_user_id": "OU1"},
                    }
                }
            )
        }
    )
    await users.reconcile()
    async with async_session() as session:
        slack = await session.one_or_none(
            UserProfile,
            expressions=[UserProfile["channel_user_id"] == "U1"],
        )
        lark = await session.one_or_none(
            UserProfile,
            expressions=[UserProfile["channel_user_id"] == "OU1"],
        )
    assert slack is not None and lark is not None
    manager = OAuthManager(
        users=users,
        encryption_key=ENCRYPTION_KEY,
        connectors=[OAuthConnector(id="github", flow=FakeDeviceFlow())],
    )
    authorization = await manager.start(slack, "github")

    with pytest.raises(ValueError, match="unknown OAuth operation"):
        await manager.complete(lark, authorization.operation_id)


async def test_github_connect_tool_emits_only_the_link_and_code() -> None:
    users, profile = await linked_user_manager()
    manager = OAuthManager(
        users=users,
        encryption_key=ENCRYPTION_KEY,
    )
    github = GitHubCapability(
        manager=manager,
        connector=manager.register(
            OAuthConnector(id=GITHUB_CONNECTOR_ID, flow=FakeDeviceFlow())
        ),
    )
    capability = await github.for_profile(profile)
    assert capability is not None
    assert capability.toolset is not None
    connect = capability.toolset.tools["connect_github"].function
    assert set(inspect.signature(connect).parameters) == {"ctx"}

    result = await connect(cast(RunContext[None], None))

    # The link and code travel as an authorization for the channel to present, never
    # in the return value the model reads back.
    assert isinstance(result, ToolReturn)
    [event] = result.metadata
    assert isinstance(event, OAuthAuthorizationEvent)
    assert event.connector_id == "github"
    assert event.label == "GitHub"
    assert event.verification_uri == "https://github.com/login/device"
    assert event.user_code == "ABCD-EFGH"
    model_sees = str(result.return_value)
    assert "ABCD-EFGH" not in model_sees
    assert "operation" not in model_sees.lower()


async def test_github_confirm_tool_asks_the_model_to_connect_first() -> None:
    # Confirming before connecting is an ordering the model can fix itself, so it
    # comes back as a retry naming `connect_github` rather than ending the turn.
    users, profile = await linked_user_manager()
    manager = OAuthManager(users=users, encryption_key=ENCRYPTION_KEY)
    github = GitHubCapability(
        manager=manager,
        connector=manager.register(
            OAuthConnector(id=GITHUB_CONNECTOR_ID, flow=FakeDeviceFlow())
        ),
    )
    capability = await github.for_profile(profile)
    assert capability is not None
    assert isinstance(capability.toolset, FunctionToolset)
    confirm = capability.toolset.tools["confirm_github"].function

    with pytest.raises(ModelRetry, match="connect_github"):
        await confirm(cast(RunContext[None], None))


async def test_github_confirm_tool_activates_the_connection() -> None:
    users, profile = await linked_user_manager()
    manager = OAuthManager(
        users=users,
        encryption_key=ENCRYPTION_KEY,
    )
    github = GitHubCapability(
        manager=manager,
        connector=manager.register(
            OAuthConnector(id=GITHUB_CONNECTOR_ID, flow=FakeDeviceFlow())
        ),
    )
    await manager.start(profile, "github")
    capability = await github.for_profile(profile)
    assert capability is not None
    assert capability.toolset is not None
    confirm = capability.toolset.tools["confirm_github"].function

    result = await confirm(cast(RunContext[None], None))

    # Nothing secret to hide here, so the outcome is the model's to relay.
    assert "@luhui" in result
    token = await manager.access_token(profile, "github")
    assert token is not None


async def _connected(
    flow: FakeDeviceFlow | None = None,
) -> tuple[OAuthManager, UserProfile, GitHubCapability]:
    """A registered user who has finished a device authorization."""
    users, profile = await linked_user_manager()
    manager = OAuthManager(users=users, encryption_key=ENCRYPTION_KEY)
    connector = manager.register(
        OAuthConnector(id=GITHUB_CONNECTOR_ID, flow=flow or FakeDeviceFlow())
    )
    await manager.start(profile, GITHUB_CONNECTOR_ID)
    await manager.complete_latest(profile, GITHUB_CONNECTOR_ID)
    github = GitHubCapability(manager=manager, connector=connector)
    return manager, profile, github


async def test_an_unauthorized_mcp_response_retires_the_connection() -> None:
    manager, profile, github = await _connected()
    capability = await github.for_profile(profile)
    assert capability is not None
    assert capability.access_token is not None
    auth = _mcp_auth(capability)

    await _drive_auth(auth, status=401)

    # The provider is the only thing that can say a token is gone, so the session
    # that heard it is what records it.
    assert await manager.access_token(profile, GITHUB_CONNECTOR_ID) is None


async def test_a_retired_connection_offers_to_authorize_again() -> None:
    manager, profile, github = await _connected()
    connected = await github.for_profile(profile)
    assert connected is not None

    # `_mcp_auth` only resolves against a real MCP session, so reaching it is the
    # assertion that this run had one.
    await _drive_auth(_mcp_auth(connected), status=401)
    after = await github.for_profile(profile)

    # The next run finds no usable connection and offers the way back rather than
    # tools that can only fail.
    assert after is not None
    assert after.access_token is None
    assert isinstance(after.toolset, FunctionToolset)
    assert set(after.toolset.tools) == {"connect_github", "confirm_github"}


async def test_a_retired_connection_is_told_apart_from_never_connecting() -> None:
    manager, profile, github = await _connected()
    connected = await github.for_profile(profile)
    assert connected is not None

    await _drive_auth(_mcp_auth(connected), status=401)
    after = await github.for_profile(profile)

    # A user who was connected a moment ago cannot see that they no longer are, so
    # the model is told to raise it rather than wait to be asked.
    assert after is not None
    assert after.connection_retired
    assert after.get_instructions() == GITHUB_RETIRED_INSTRUCTION


async def test_never_connecting_reads_as_itself() -> None:
    users, profile = await linked_user_manager()
    manager = OAuthManager(users=users, encryption_key=ENCRYPTION_KEY)
    github = GitHubCapability(
        manager=manager,
        connector=manager.register(
            OAuthConnector(id=GITHUB_CONNECTOR_ID, flow=FakeDeviceFlow())
        ),
    )

    capability = await github.for_profile(profile)

    assert capability is not None
    assert not capability.connection_retired
    assert capability.get_instructions() == GITHUB_OAUTH_INSTRUCTION


async def test_reconnecting_stops_the_stale_warning() -> None:
    manager, profile, github = await _connected()
    connected = await github.for_profile(profile)
    assert connected is not None
    await _drive_auth(_mcp_auth(connected), status=401)

    await manager.start(profile, GITHUB_CONNECTOR_ID)
    await manager.complete_latest(profile, GITHUB_CONNECTOR_ID)
    after = await github.for_profile(profile)

    assert after is not None
    assert after.access_token is not None
    assert not after.connection_retired
    assert after.get_instructions() is None


async def test_an_ordinary_mcp_failure_leaves_the_connection_alone() -> None:
    manager, profile, github = await _connected()
    capability = await github.for_profile(profile)
    assert capability is not None

    await _drive_auth(_mcp_auth(capability), status=500)

    # A server that broke says nothing about the credential it was handed.
    assert await manager.access_token(profile, GITHUB_CONNECTOR_ID) is not None


async def test_the_token_still_reaches_the_provider() -> None:
    _manager, profile, github = await _connected()
    capability = await github.for_profile(profile)
    assert capability is not None
    assert capability.access_token is not None

    request = await _drive_auth(_mcp_auth(capability), status=200)

    assert request.headers["Authorization"] == (
        f"Bearer {capability.access_token.get_secret_value()}"
    )


async def test_an_expired_connection_records_itself_on_the_way_out() -> None:
    manager, profile, github = await _connected()
    async with async_session() as session:
        connection = await session.one_or_none(
            OAuthConnection,
            expressions=[OAuthConnection["connector_id"] == GITHUB_CONNECTOR_ID],
        )
        assert connection is not None
        connection.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()

    assert await manager.access_token(profile, GITHUB_CONNECTOR_ID) is None

    # Expiry is the one death the row can announce by itself; reading it once is
    # enough to stop it being offered again.
    async with async_session() as session:
        connection = await session.one_or_none(
            OAuthConnection,
            expressions=[OAuthConnection["connector_id"] == GITHUB_CONNECTOR_ID],
        )
        assert connection is not None
        assert connection.status == "invalid"
