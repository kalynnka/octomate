from __future__ import annotations

import asyncio
import contextlib
import inspect
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
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
from octomate.capabilities.harness.events import (
    OAuthAuthorizationEvent,
    OAuthDeviceAuthorizationEvent,
)
from octomate.capabilities.harness.mcp import OAuthMcpCapability
from octomate.capabilities.linear import LinearCapability
from octomate.config.users import UserConfig
from octomate.database import async_session
from octomate.managers.oauth import (
    OAuthConnector,
    OAuthManager,
    UnusableOAuthOperation,
)
from octomate.managers.user import UserManager
from octomate.oauth.base import McpConnectionAuth
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

GITHUB_CONNECTOR_ID = "github"
LINEAR_CONNECTOR_ID = "linear"
URL_ADAPTER = TypeAdapter(AnyHttpUrl)
HTTPS_URL_ADAPTER = TypeAdapter(HttpsUrl)
ENCRYPTION_KEY = SecretStr(urlsafe_b64encode(bytes(range(32))).decode())


@pytest.fixture(autouse=True)
async def database(in_memory_engine: AsyncEngine) -> None:
    return


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
            expires_at=datetime.now(UTC) + self.lifetime,
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
        self.state: SecretStr | None = None
        self.exchanges: list[tuple[str, str | None]] = []
        # How long the link this flow hands out stays good; negative to mint one
        # that is already past its deadline.
        self.lifetime = timedelta(minutes=10)
        self.grant = OAuthGrant(
            access_token=SecretStr("linear-token"),
            refresh_token=SecretStr("linear-refresh"),
            subject="usr_42",
            account_label="Alice",
            scopes=["read", "write"],
        )
        self.refreshed = OAuthGrant(
            access_token=SecretStr("linear-token-2"),
            refresh_token=SecretStr("linear-refresh-2"),
            subject="usr_42",
            account_label="Alice",
            scopes=["read", "write"],
        )
        # Set to refuse the refresh the way a provider that ended the grant does.
        self.refresh_refused = False
        # Set to fail the token exchange the way a misconfigured client does.
        self.exchange_refused: Exception | None = None

    async def start(
        self,
        context: OAuthFlowContext,
        callback_uri: AnyHttpUrl,
        state: SecretStr,
    ) -> AuthorizationRequest:
        self.context = context
        self.callback = callback_uri
        self.state = state
        return AuthorizationRequest(
            authorization_uri=URL_ADAPTER.validate_python(
                "https://example.com/authorize?state=provider-secret-state"
            ),
            code_verifier=SecretStr("pkce-verifier"),
            expires_at=datetime.now(UTC) + self.lifetime,
        )

    async def exchange(
        self,
        context: OAuthFlowContext,
        *,
        code: str,
        code_verifier: SecretStr | None,
        callback_uri: AnyHttpUrl,
    ) -> OAuthGrant:
        self.context = context
        self.exchanges.append(
            (code, code_verifier.get_secret_value() if code_verifier else None)
        )
        if self.exchange_refused is not None:
            raise self.exchange_refused
        return self.grant

    async def refresh(self, refresh_token: SecretStr) -> OAuthGrant:
        if self.refresh_refused:
            raise ValueError("Linear authorization failed: refresh token is spent")
        assert refresh_token.get_secret_value() == "linear-refresh"
        return self.refreshed


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


def direct_http() -> DirectHttpOAuthCallbackTransport:
    return DirectHttpOAuthCallbackTransport(
        URL_ADAPTER.validate_python("http://127.0.0.1:8000")
    )


async def linear_manager(
    flow: FakeAuthorizationCodeFlow | None = None,
) -> tuple[OAuthManager, UserProfile, FakeAuthorizationCodeFlow]:
    """A registered user and a Linear connector on the real direct-HTTP transport."""
    users, profile = await linked_user_manager()
    flow = flow or FakeAuthorizationCodeFlow()
    manager = OAuthManager(
        users=users,
        encryption_key=ENCRYPTION_KEY,
        connectors=[
            OAuthConnector(
                id=LINEAR_CONNECTOR_ID,
                flow=flow,
                callback_transport=direct_http(),
            )
        ],
    )
    return manager, profile, flow


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


def _mcp_auth(capability: OAuthMcpCapability) -> McpConnectionAuth:
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
            callback_transport=direct_http(),
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
    manager, profile, flow = await linear_manager()

    authorization = await manager.start(profile, LINEAR_CONNECTOR_ID)

    assert isinstance(authorization, AuthorizationLink)
    assert flow.callback == URL_ADAPTER.validate_python(
        "http://127.0.0.1:8000/oauth/linear/callback"
    )
    assert authorization.authorization_uri == URL_ADAPTER.validate_python(
        f"http://127.0.0.1:8000/oauth/linear/start/{authorization.operation_id}"
    )


async def test_the_user_facing_link_carries_only_the_operation_uuid() -> None:
    manager, profile, flow = await linear_manager()

    authorization, _ = await started(manager, profile, flow)
    staged = await manager.staged_authorization(
        LINEAR_CONNECTOR_ID, authorization.operation_id
    )

    # The provider request and everything in it is reachable only by opening the
    # link; nothing of it travels in the link itself.
    assert "provider-secret-state" in str(staged.authorization_uri)
    assert "provider-secret-state" not in str(authorization.authorization_uri)
    assert flow.state is not None
    assert flow.state.get_secret_value() not in str(authorization.authorization_uri)
    # The state names its operation but is not the operation id: the id is public.
    assert flow.state.get_secret_value().startswith(f"{authorization.operation_id}.")
    assert flow.state.get_secret_value() != str(authorization.operation_id)


async def test_a_started_authorization_seals_its_state_and_verifier() -> None:
    manager, profile, _ = await linear_manager()

    await manager.start(profile, LINEAR_CONNECTOR_ID)

    async with async_session() as session:
        [operation] = await session.list(OAuthOperation, limit=None)
    assert b"pkce-verifier" not in operation.encrypted_data
    assert b"provider-secret-state" not in operation.encrypted_data
    # Nothing to poll: an authorization-code operation is finished by its callback.
    assert operation.interval_seconds is None


async def test_authorization_code_connector_can_select_a_relay() -> None:
    users, profile = await linked_user_manager()
    manager = OAuthManager(
        users=users,
        encryption_key=ENCRYPTION_KEY,
        connectors=[
            OAuthConnector(
                id=LINEAR_CONNECTOR_ID,
                flow=FakeAuthorizationCodeFlow(),
                callback_transport=FakeRelayTransport(),
            )
        ],
    )

    authorization = await manager.start(profile, LINEAR_CONNECTOR_ID)

    assert isinstance(authorization, AuthorizationLink)
    assert str(authorization.authorization_uri).startswith(
        "https://relay.example/oauth/start/"
    )


async def started(
    manager: OAuthManager,
    profile: UserProfile,
    flow: FakeAuthorizationCodeFlow,
) -> tuple[AuthorizationLink, str]:
    """Start an authorization and read back the state only the provider would know."""
    authorization = await manager.start(profile, LINEAR_CONNECTOR_ID)
    assert isinstance(authorization, AuthorizationLink)
    assert flow.state is not None
    return authorization, flow.state.get_secret_value()


async def test_the_callback_stores_an_owner_bound_encrypted_token() -> None:
    manager, profile, flow = await linear_manager()
    _, state = await started(manager, profile, flow)

    grant = await manager.complete_callback(
        LINEAR_CONNECTOR_ID, state=state, code="auth-code"
    )

    assert grant.account_label == "Alice"
    # The verifier the operation was holding is what the exchange spent.
    assert flow.exchanges == [("auth-code", "pkce-verifier")]
    token = await manager.access_token(profile, LINEAR_CONNECTOR_ID)
    assert token is not None
    assert token.get_secret_value() == "linear-token"
    async with async_session() as session:
        [connection] = await session.list(OAuthConnection, limit=None)
    assert b"linear-token" not in connection.encrypted_tokens


async def test_a_replayed_callback_is_refused() -> None:
    manager, profile, flow = await linear_manager()
    _, state = await started(manager, profile, flow)
    await manager.complete_callback(LINEAR_CONNECTOR_ID, state=state, code="auth-code")

    with pytest.raises(UnusableOAuthOperation, match="already been consumed"):
        await manager.complete_callback(
            LINEAR_CONNECTOR_ID, state=state, code="auth-code"
        )

    # One exchange, not two: the replay never reached the provider.
    assert flow.exchanges == [("auth-code", "pkce-verifier")]


async def test_an_expired_authorization_cannot_be_completed() -> None:
    flow = FakeAuthorizationCodeFlow()
    flow.lifetime = timedelta(seconds=-1)
    manager, profile, flow = await linear_manager(flow)
    _, state = await started(manager, profile, flow)

    with pytest.raises(UnusableOAuthOperation, match="expired"):
        await manager.complete_callback(
            LINEAR_CONNECTOR_ID, state=state, code="auth-code"
        )

    assert flow.exchanges == []


async def test_a_guessed_state_cannot_finish_an_authorization() -> None:
    manager, profile, flow = await linear_manager()
    authorization, _ = await started(manager, profile, flow)

    # The operation id is public — it is in the link the user opened — so naming it
    # has to be worth nothing without the random half of the state.
    with pytest.raises(UnusableOAuthOperation, match="does not match"):
        await manager.complete_callback(
            LINEAR_CONNECTOR_ID,
            state=f"{authorization.operation_id}.guessed",
            code="auth-code",
        )

    assert flow.exchanges == []
    assert await manager.access_token(profile, LINEAR_CONNECTOR_ID) is None


async def test_another_connectors_operation_is_not_reachable() -> None:
    manager, profile, flow = await linear_manager()
    _, state = await started(manager, profile, flow)

    with pytest.raises(UnusableOAuthOperation, match="unknown OAuth operation"):
        await manager.complete_callback("github", state=state, code="auth-code")


async def test_a_declined_authorization_is_closed() -> None:
    manager, profile, flow = await linear_manager()
    _, state = await started(manager, profile, flow)

    await manager.abandon_callback(LINEAR_CONNECTOR_ID, state=state)

    # Declining is an answer, so the link the user turned down stops working now
    # rather than at the end of its lifetime.
    with pytest.raises(UnusableOAuthOperation, match="already been consumed"):
        await manager.complete_callback(
            LINEAR_CONNECTOR_ID, state=state, code="auth-code"
        )
    assert await manager.connection_status(profile, LINEAR_CONNECTOR_ID) is None


async def test_unlinking_the_profile_stops_its_callback() -> None:
    manager, profile, flow = await linear_manager()
    _, state = await started(manager, profile, flow)
    # The YAML declaration goes away while the user is at the provider's page.
    manager.users.config = {}
    await manager.users.reconcile()

    with pytest.raises(UnusableOAuthOperation, match="no longer linked"):
        await manager.complete_callback(
            LINEAR_CONNECTOR_ID, state=state, code="auth-code"
        )

    assert flow.exchanges == []


async def test_a_near_expiry_token_is_refreshed_before_it_is_used() -> None:
    manager, profile, flow = await linear_manager()
    _, state = await started(manager, profile, flow)
    flow.grant = flow.grant.model_copy(
        update={"expires_at": datetime.now(UTC) + timedelta(seconds=30)}
    )
    await manager.complete_callback(LINEAR_CONNECTOR_ID, state=state, code="auth-code")

    token = await manager.access_token(profile, LINEAR_CONNECTOR_ID)

    # Spent while there is still room to replace it: a token that dies mid-run fails
    # the same way a revoked one does.
    assert token is not None
    assert token.get_secret_value() == "linear-token-2"
    async with async_session() as session:
        [connection] = await session.list(OAuthConnection, limit=None)
    assert connection.status == "active"


async def test_a_refused_refresh_retires_the_connection() -> None:
    manager, profile, flow = await linear_manager()
    _, state = await started(manager, profile, flow)
    flow.grant = flow.grant.model_copy(
        update={"expires_at": datetime.now(UTC) + timedelta(seconds=30)}
    )
    await manager.complete_callback(LINEAR_CONNECTOR_ID, state=state, code="auth-code")
    flow.refresh_refused = True

    assert await manager.access_token(profile, LINEAR_CONNECTOR_ID) is None

    # A provider that will not renew the grant has ended it, which reads the same as
    # the 401 a revoked token answers with.
    assert await manager.connection_status(profile, LINEAR_CONNECTOR_ID) == "invalid"


async def test_concurrent_reads_spend_one_refresh_token_once() -> None:
    manager, profile, flow = await linear_manager()
    _, state = await started(manager, profile, flow)
    flow.grant = flow.grant.model_copy(
        update={"expires_at": datetime.now(UTC) + timedelta(seconds=30)}
    )
    await manager.complete_callback(LINEAR_CONNECTOR_ID, state=state, code="auth-code")

    tokens = await asyncio.gather(
        *(manager.access_token(profile, LINEAR_CONNECTOR_ID) for _ in range(4))
    )

    # A rotating refresh token is spendable once, so the runs that lost the race
    # have to find the winner's result rather than send the spent one upstream —
    # `FakeAuthorizationCodeFlow.refresh` asserts it is only ever handed the first.
    assert {token.get_secret_value() for token in tokens if token} == {"linear-token-2"}


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
    assert slack is not None
    assert lark is not None
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
    assert isinstance(capability.toolset, FunctionToolset)
    connect = capability.toolset.tools["connect_github"].function
    assert set(inspect.signature(connect).parameters) == {"ctx"}

    result = await connect(cast(RunContext[None], None))

    # The link and code travel as an authorization for the channel to present, never
    # in the return value the model reads back.
    assert isinstance(result, ToolReturn)
    [event] = result.metadata
    assert isinstance(event, OAuthDeviceAuthorizationEvent)
    assert event.connector_id == "github"
    assert event.label == "GitHub"
    assert event.authorization_uri == "https://github.com/login/device"
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
    assert isinstance(capability.toolset, FunctionToolset)
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
        connection.expires_at = datetime.now(UTC) - timedelta(seconds=1)
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


async def linear_capability() -> tuple[
    OAuthManager, UserProfile, FakeAuthorizationCodeFlow, LinearCapability
]:
    manager, profile, flow = await linear_manager()
    return (
        manager,
        profile,
        flow,
        LinearCapability(
            manager=manager,
            connector=manager.connector(LINEAR_CONNECTOR_ID),
        ),
    )


async def test_linear_connect_tool_emits_a_link_and_no_code() -> None:
    _manager, profile, _flow, linear = await linear_capability()
    capability = await linear.for_profile(profile)
    assert capability is not None
    assert isinstance(capability.toolset, FunctionToolset)
    connect = capability.toolset.tools["connect_linear"].function
    assert set(inspect.signature(connect).parameters) == {"ctx"}

    result = await connect(cast(RunContext[None], None))

    assert isinstance(result, ToolReturn)
    [event] = result.metadata
    assert isinstance(event, OAuthAuthorizationEvent)
    assert event.connector_id == LINEAR_CONNECTOR_ID
    assert event.label == "Linear"
    # Nothing for the user to type, so a presenter is never handed a code at all —
    # and the provider's own request never reaches the channel.
    assert not hasattr(event, "user_code")
    assert event.authorization_uri.startswith(
        "http://127.0.0.1:8000/oauth/linear/start/"
    )
    assert "provider-secret-state" not in str(result.return_value)


async def test_linear_confirm_tool_reports_without_finishing_anything() -> None:
    manager, profile, flow, linear = await linear_capability()
    capability = await linear.for_profile(profile)
    assert capability is not None
    assert isinstance(capability.toolset, FunctionToolset)
    confirm = capability.toolset.tools["confirm_linear"].function
    assert set(inspect.signature(confirm).parameters) == {"ctx"}

    waiting = await confirm(cast(RunContext[None], None))

    # The browser finishes this connection; confirming only looks.
    assert "not connected yet" in waiting
    _, state = await started(manager, profile, flow)
    await manager.complete_callback(LINEAR_CONNECTOR_ID, state=state, code="auth-code")

    assert "connected" in await confirm(cast(RunContext[None], None))


async def test_a_connected_user_gets_the_linear_mcp_toolset() -> None:
    manager, profile, flow, linear = await linear_capability()
    _, state = await started(manager, profile, flow)
    await manager.complete_callback(LINEAR_CONNECTOR_ID, state=state, code="auth-code")

    capability = await linear.for_profile(profile)

    assert capability is not None
    assert capability.access_token is not None
    request = await _drive_auth(_mcp_auth(capability), status=200)
    assert request.headers["Authorization"] == "Bearer linear-token"


async def test_a_visitor_gets_no_linear_tools() -> None:
    manager, _profile, _flow, linear = await linear_capability()
    visitor = await manager.users.ensure_profile(
        "slack",
        UserProfile(channel_user_id="visitor", name="Visitor"),
    )

    assert await linear.for_profile(visitor) is None


def test_a_capability_refuses_a_connector_of_the_wrong_flow() -> None:
    manager = OAuthManager(users=UserManager(), encryption_key=ENCRYPTION_KEY)
    device = manager.register(
        OAuthConnector(id=GITHUB_CONNECTOR_ID, flow=FakeDeviceFlow())
    )

    with pytest.raises(ValueError, match="AuthorizationCodeOAuthFlow"):
        LinearCapability(manager=manager, connector=device)
