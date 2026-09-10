from __future__ import annotations

import asyncio
from base64 import urlsafe_b64encode
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import httpx2
import pytest
from cryptography.exceptions import InvalidTag
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from pydantic import JsonValue, SecretStr, TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import McpPoolConfig, OctomateConfig
from octomate.database import async_session
from octomate.managers.mcp import McpClientKey, McpManager, McpUnavailable
from octomate.managers.user import UserManager
from octomate.mcp.server import CALL_MCP_TOOL, LIST_MCPS, tentacles_mcp
from octomate.schemas.mcp import (
    BearerMcp,
    Mcp,
    McpInstallRequest,
    McpVariant,
    OAuthMcp,
)
from octomate.schemas.oauth import OAuthCipher, OAuthConnection
from octomate.schemas.user import User, UserProfile
from octomate.tentacles.claude.mcp import sdk_tool
from tests.agent.test_mcp import a_turn, an_upstream, upstream_of
from tests.channels.slack.test_mcp import into
from tests.support.managers import fixed_session
from tests.support.mcp import discover
from tests.support.users import a_user


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


@pytest.fixture
def manager() -> McpManager:
    return McpManager(
        UserManager(),
        OAuthCipher(SecretStr(urlsafe_b64encode(bytes(range(32))).decode())),
    )


def install_request(token: str | None = None) -> McpInstallRequest:
    return McpInstallRequest.model_validate(
        {
            "name": "Private research",
            "namespace": "research",
            "url": "https://mcp.example/mcp",
            "auth": {"kind": "bearer", "token": token} if token else {"kind": "none"},
        }
    )


class Requests(httpx2.AsyncBaseTransport):
    def __init__(self, upstream: httpx2.AsyncBaseTransport) -> None:
        self.upstream = upstream
        self.methods: list[str] = []
        self.closed = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            body = TypeAdapter(dict[str, JsonValue]).validate_json(
                await request.aread()
            )
            method = body.get("method")
            if isinstance(method, str):
                self.methods.append(method)
        return await self.upstream.handle_async_request(request)

    async def aclose(self) -> None:
        self.closed += 1


@asynccontextmanager
async def connected(manager: McpManager, upstream: FastMCP) -> AsyncIterator[Requests]:
    async with upstream_of(upstream) as transport:
        requests = Requests(transport)
        manager.httpx_client_factory = into(requests)
        async with manager.lifespan():
            yield requests


async def test_instances_are_owner_bound_and_credentials_encrypted(
    manager: McpManager,
) -> None:
    alice, bob = await a_user("alice"), await a_user("bob")
    first = await manager.install(alice.id, install_request("alice-secret"))
    second = await manager.install(bob.id, install_request("bob-secret"))
    assert first.namespace == second.namespace == "personal/research"
    restarted = McpManager(manager.users, manager.cipher)
    stored = (await restarted.list(alice.id))[0]
    assert isinstance(stored, BearerMcp)
    assert stored.encrypted_token is not None
    assert b"alice-secret" not in stored.encrypted_token
    assert "encrypted_token" not in stored.model_dump()
    assert "alice-secret" not in repr(stored)
    assert manager.cipher is not None
    assert (
        manager.cipher.decrypt(
            stored.encrypted_token, context=f"mcp:{alice.id}:{first.id}"
        )
        == "alice-secret"
    )
    with pytest.raises(InvalidTag):
        manager.cipher.decrypt(
            stored.encrypted_token, context=f"mcp:{bob.id}:{first.id}"
        )
    with pytest.raises(ValueError, match="already installed"):
        await manager.install(alice.id, install_request())
    with pytest.raises(McpUnavailable):
        await manager.disable(user_id=bob.id, mcp_id=first.id)
    with pytest.raises(McpUnavailable):
        await manager.enable(user_id=bob.id, mcp_id=first.id)
    with pytest.raises(McpUnavailable):
        await manager.uninstall(bob.id, first.id)
    await manager.uninstall(alice.id, first.id)
    assert await manager.list(alice.id) == []
    assert [item.id for item in await manager.list(bob.id)] == [second.id]


async def test_bearer_install_requires_encryption_and_user_deletion_cascades(
    manager: McpManager,
) -> None:
    user = await a_user()
    with pytest.raises(ValueError, match="encryption_key"):
        await McpManager(manager.users, None).install(
            user.id, install_request("secret")
        )
    instance = await manager.install(user.id, install_request())
    async with async_session() as session:
        stored = await session.get(User, user.id)
        assert stored is not None
        await session.delete(stored)
        await session.commit()
    async with async_session() as session:
        assert await session.get(Mcp, instance.id) is None


@pytest.mark.parametrize("linked", [False, True])
async def test_oauth_instance_roundtrips_without_unauthenticated_upstream_requests(
    manager: McpManager, linked: bool
) -> None:
    user = await a_user()
    grant = OAuthConnection(
        user_id=user.id,
        connector_id="research",
        encrypted_tokens=b"opaque encrypted grant",
        subject="research-user",
        account_label="Research",
    )
    instance = OAuthMcp(
        user_id=user.id,
        name="Research",
        namespace="personal/research",
        url="https://mcp.example/mcp",
        tentacle_id="research",
    )
    async with async_session() as session:
        session.add(instance)
        await session.flush()
        if linked:
            grant.mcp_id = instance.id
            session.add(grant)
        await session.commit()

    stored = (await manager.list(user.id))[0]
    assert isinstance(stored, OAuthMcp)
    assert stored.tentacle_id == "research"
    serialized = TypeAdapter(McpVariant).dump_python(stored, mode="json")
    assert serialized["auth_kind"] == "oauth"
    assert "encrypted_token" not in serialized
    assert "encrypted_tokens" not in serialized

    upstream, _ = an_upstream("answer")
    async with connected(manager, upstream) as requests:
        with pytest.raises(McpUnavailable):
            async with manager.acquire(
                a_turn(UserProfile(user_id=user.id)), instance.namespace
            ):
                pytest.fail("OAuth instance reached upstream without OAuth support")
        assert requests.methods == []

    if linked:
        async with async_session() as session:
            connection = await session.get(OAuthConnection, grant.id)
            assert connection is not None
            await session.delete(connection)
            await session.commit()
        remaining = (await manager.list(user.id))[0]
        assert isinstance(remaining, OAuthMcp)
        assert remaining.tentacle_id == "research"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/mcp",
        "https://a:b@example.com/mcp",
        "https://example.com/mcp?token=secret",
        "https://example.com/mcp#secret",
    ],
)
def test_endpoint_rejects_insecure_or_secret_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        McpInstallRequest.model_validate(
            {"name": "Example", "namespace": "example", "url": url}
        )


async def test_client_is_reused_across_turns_and_closed_at_shutdown(
    manager: McpManager,
) -> None:
    user = await a_user()
    instance = await manager.install(user.id, install_request("alice"))
    upstream, calls = an_upstream("answer")
    async with connected(manager, upstream) as requests:
        assert manager.clients == {}
        first_scope = a_turn(UserProfile(user_id=user.id))
        async with manager.acquire(first_scope, instance.namespace) as first:
            assert manager.clients == {
                McpClientKey(user_id=user.id, mcp_id=instance.id): first
            }
        for channel in ("slack", "claude"):
            scope = a_turn(UserProfile(user_id=user.id, channel_tentacle_id=channel))
            async with manager.acquire(scope, instance.namespace) as reused:
                assert reused is first
            server = tentacles_mcp(fixed_session(scope), manager=manager)
            for _ in range(2):
                catalog = await discover(server, instance.namespace)
                assert [tool.name for tool in catalog.tools] == ["answer"]
                await server.call_tool(
                    CALL_MCP_TOOL,
                    {
                        "namespace": instance.namespace,
                        "name": "answer",
                        "arguments": {},
                    },
                )
        assert requests.methods.count("server/discover") == 0
        assert requests.methods.count("initialize") == 0
        assert requests.methods.count("tools/list") == 4
        assert requests.methods.count("tools/call") == 4
        assert requests.closed == 0
    assert requests.closed == 1
    assert manager.clients == {}
    assert calls == ["Bearer alice"] * 4


async def test_private_discovery_and_cached_calls_recheck_access(
    manager: McpManager,
) -> None:
    alice, bob = await a_user("alice"), await a_user("bob")
    alice_scope, bob_scope = [
        a_turn(UserProfile(user_id=user.id)) for user in (alice, bob)
    ]
    alice_server, bob_server, visitor_server = [
        tentacles_mcp(fixed_session(scope), manager=manager)
        for scope in (alice_scope, bob_scope, a_turn())
    ]
    # Servers are built before the instance, just as the shared HTTP mount is.
    installed = await manager.install(alice.id, install_request("alice"))
    upstream, calls = an_upstream("answer")
    async with connected(manager, upstream) as requests:
        for server in (bob_server, visitor_server):
            assert (await server.call_tool(LIST_MCPS, {})).structured_content == {
                "result": []
            }
            with pytest.raises(ToolError, match="unavailable"):
                await discover(server, installed.namespace)
        assert requests.methods == []
        listed = await alice_server.call_tool(LIST_MCPS, {})
        assert "Private research" in str(listed.structured_content)
        assert "Private research" not in (alice_server.instructions or "")
        tool = await alice_server.get_tool(CALL_MCP_TOOL)
        assert tool is not None
        cached = sdk_tool(tool)
        args: dict[str, JsonValue] = {
            "namespace": installed.namespace,
            "name": "answer",
            "arguments": {},
        }
        await cached.handler(args)
        key = McpClientKey(user_id=alice.id, mcp_id=installed.id)
        retained = manager.clients[key]
        enabled = await manager.enable(user_id=alice.id, mcp_id=installed.id)
        assert enabled.enabled is True
        assert manager.clients[key] is retained
        assert requests.closed == 0
        await manager.disable(user_id=alice.id, mcp_id=installed.id)
        assert requests.closed == 1
        assert (await cached.handler(args))["is_error"] is True
        await manager.enable(user_id=alice.id, mcp_id=installed.id)
        await cached.handler(args)
        await manager.uninstall(alice.id, installed.id)
        assert (await cached.handler(args))["is_error"] is True
        assert requests.methods.count("tools/call") == 2
    assert calls == ["Bearer alice", "Bearer alice"]


async def test_concurrent_users_never_share_credentials(manager: McpManager) -> None:
    alice, bob = await a_user("alice"), await a_user("bob")
    for user in (alice, bob):
        await manager.install(user.id, install_request(user.username))
    upstream, calls = an_upstream("answer")
    servers = [
        tentacles_mcp(
            fixed_session(a_turn(UserProfile(user_id=user.id))), manager=manager
        )
        for user in (alice, bob)
    ]
    async with connected(manager, upstream) as requests:
        await asyncio.gather(
            *(
                server.call_tool(
                    CALL_MCP_TOOL,
                    {
                        "namespace": "personal/research",
                        "name": "answer",
                        "arguments": {},
                    },
                )
                for server in servers
                for _ in range(2)
            )
        )
        assert len(manager.clients) == 2
        assert len({id(client) for client in manager.clients.values()}) == 2
    assert requests.closed == 2
    assert sorted(calls) == ["Bearer alice", "Bearer alice", "Bearer bob", "Bearer bob"]


@pytest.mark.parametrize("action", ["disable", "uninstall"])
async def test_removal_cuts_off_dispatched_calls_and_rejects_new_calls(
    manager: McpManager,
    action: Literal["disable", "uninstall"],
) -> None:
    user = await a_user()
    instance = await manager.install(user.id, install_request())
    scope = a_turn(UserProfile(user_id=user.id))
    started, finish = asyncio.Event(), asyncio.Event()
    upstream = FastMCP("slow")

    @upstream.tool
    async def slow() -> str:
        started.set()
        await finish.wait()
        return "finished"

    async def call() -> None:
        async with manager.acquire(scope, instance.namespace) as client:
            await client.call_tool_mcp("slow", {})

    async with connected(manager, upstream) as requests:
        task = asyncio.create_task(call())
        await asyncio.wait_for(started.wait(), 5)
        try:
            if action == "disable":
                await manager.disable(user_id=user.id, mcp_id=instance.id)
            else:
                await manager.uninstall(user_id=user.id, mcp_id=instance.id)
            assert requests.closed == 1
            assert manager.clients == {}
            with pytest.raises(McpUnavailable):
                async with manager.acquire(scope, instance.namespace):
                    pytest.fail("removed MCP was acquired")
            with pytest.raises(MCPError, match="Connection closed"):
                await asyncio.wait_for(task, timeout=5)
        finally:
            finish.set()


async def test_catalog_changes_are_visible_on_retained_connection(
    manager: McpManager,
) -> None:
    user = await a_user()
    instance = await manager.install(user.id, install_request())
    server = tentacles_mcp(
        fixed_session(a_turn(UserProfile(user_id=user.id))), manager=manager
    )
    upstream, _ = an_upstream("answer")
    async with connected(manager, upstream) as requests:
        assert [
            tool.name for tool in (await discover(server, instance.namespace)).tools
        ] == ["answer"]

        @upstream.tool
        def another() -> str:
            return "new tool"

        assert {
            tool.name for tool in (await discover(server, instance.namespace)).tools
        } == {"answer", "another"}
        assert len(manager.clients) == 1
        assert requests.methods.count("tools/list") == 2


async def test_overlapping_turns_share_the_same_client(manager: McpManager) -> None:
    user = await a_user()
    instance = await manager.install(user.id, install_request())
    scopes = [a_turn(UserProfile(user_id=user.id)) for _ in range(2)]
    upstream, _ = an_upstream("answer")
    async with connected(manager, upstream) as requests:
        async with manager.acquire(scopes[0], instance.namespace) as first:
            async with manager.acquire(scopes[1], instance.namespace) as second:
                assert first is second
                await first.list_tools()
                await second.list_tools()
        assert requests.closed == 0
    assert requests.closed == 1


async def test_cancelling_a_borrower_releases_its_reference(
    manager: McpManager,
) -> None:
    user = await a_user()
    instance = await manager.install(user.id, install_request())
    scope = a_turn(UserProfile(user_id=user.id))
    upstream, _ = an_upstream("answer")
    ready = asyncio.Event()

    async def borrow() -> None:
        async with manager.acquire(scope, instance.namespace):
            ready.set()
            await asyncio.Future[None]()

    async with connected(manager, upstream) as requests:
        task = asyncio.create_task(borrow())
        await asyncio.wait_for(ready.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        key = McpClientKey(user_id=user.id, mcp_id=instance.id)
        assert key in manager.sweaps
        await manager.uninstall(user.id, instance.id)
        assert requests.closed == 1


async def test_same_owner_mcps_and_reinstalls_have_separate_clients(
    manager: McpManager,
) -> None:
    user = await a_user()
    first = await manager.install(user.id, install_request())
    second = await manager.install(
        user.id, install_request().model_copy(update={"namespace": "other"})
    )
    scope = a_turn(UserProfile(user_id=user.id))
    upstream, _ = an_upstream("answer")
    async with connected(manager, upstream) as requests:
        async with manager.acquire(scope, first.namespace) as first_client:
            await first_client.list_tools()
        async with manager.acquire(scope, second.namespace) as second_client:
            await second_client.list_tools()
        assert first_client is not second_client
        assert manager.clients == {
            McpClientKey(user_id=user.id, mcp_id=first.id): first_client,
            McpClientKey(user_id=user.id, mcp_id=second.id): second_client,
        }
        await manager.uninstall(user.id, first.id)
        assert requests.closed == 1
        replacement = await manager.install(user.id, install_request())
        async with manager.acquire(scope, replacement.namespace) as replacement_client:
            assert replacement_client is not first_client
            await replacement_client.list_tools()
        assert manager.clients == {
            McpClientKey(user_id=user.id, mcp_id=replacement.id): replacement_client,
            McpClientKey(user_id=user.id, mcp_id=second.id): second_client,
        }
    assert requests.closed == 3


async def test_idle_client_expires_and_next_acquisition_creates_a_new_one(
    manager: McpManager,
) -> None:
    assert manager.idle_timeout == 3600
    manager.idle_timeout = 0.001
    user = await a_user()
    instance = await manager.install(user.id, install_request())
    key = McpClientKey(user_id=user.id, mcp_id=instance.id)
    scope = a_turn(UserProfile(user_id=user.id))
    upstream, _ = an_upstream("answer")
    async with connected(manager, upstream) as requests:
        async with manager.acquire(scope, instance.namespace) as first:
            assert key in manager.sweaps
        assert requests.closed == 0
        await asyncio.wait_for(manager.sweaps[key], timeout=5)
        assert requests.closed == 1
        assert manager.clients == manager.sweaps == {}
        manager.idle_timeout = McpPoolConfig().idle_timeout
        async with manager.acquire(scope, instance.namespace) as second:
            assert second is not first
            await second.list_tools()
    assert requests.closed == 2


def test_host_passes_global_mcp_idle_timeout_to_manager() -> None:
    config = OctomateConfig(mcp_pool=McpPoolConfig(idle_timeout=1200))
    host = Octomate(config=config)
    assert host.mcp.idle_timeout == 1200


async def test_each_acquisition_refreshes_cleanup(
    manager: McpManager,
) -> None:
    user = await a_user()
    instance = await manager.install(user.id, install_request())
    key = McpClientKey(user_id=user.id, mcp_id=instance.id)
    scope = a_turn(UserProfile(user_id=user.id))
    upstream, _ = an_upstream("answer")
    async with connected(manager, upstream) as requests:
        async with manager.acquire(scope, instance.namespace) as first:
            await first.list_tools()
        previous_cleanup = manager.sweaps[key]
        async with manager.acquire(scope, instance.namespace) as second:
            assert second is first
            assert previous_cleanup.cancelled()
            refreshed_cleanup = manager.sweaps[key]
            assert refreshed_cleanup is not previous_cleanup
            await asyncio.sleep(0)
            manager.idle_timeout = 0.001
            async with manager.acquire(scope, instance.namespace) as third:
                assert third is first
                assert refreshed_cleanup.cancelled()
                latest_cleanup = manager.sweaps[key]
                assert latest_cleanup is not refreshed_cleanup
            assert manager.sweaps[key] is latest_cleanup
            await asyncio.wait_for(latest_cleanup, timeout=5)
            assert not second.is_connected()
            assert requests.closed == 1
            assert manager.clients == manager.sweaps == {}


@pytest.mark.parametrize("action", ["disable", "uninstall", "shutdown"])
async def test_removal_cancels_pending_idle_cleanup(
    manager: McpManager, action: Literal["disable", "uninstall", "shutdown"]
) -> None:
    user = await a_user()
    instance = await manager.install(user.id, install_request())
    key = McpClientKey(user_id=user.id, mcp_id=instance.id)
    scope = a_turn(UserProfile(user_id=user.id))
    upstream, _ = an_upstream("answer")
    async with connected(manager, upstream) as requests:
        async with manager.acquire(scope, instance.namespace) as client:
            await client.list_tools()
        cleanup = manager.sweaps[key]
        match action:
            case "disable":
                await manager.disable(user_id=user.id, mcp_id=instance.id)
            case "uninstall":
                await manager.uninstall(user_id=user.id, mcp_id=instance.id)
            case "shutdown":
                pass
    assert cleanup.cancelled()
    assert requests.closed == 1
    assert manager.clients == manager.sweaps == {}


async def test_removed_borrower_cannot_schedule_cleanup_for_replacement(
    manager: McpManager,
) -> None:
    user = await a_user()
    instance = await manager.install(user.id, install_request())
    key = McpClientKey(user_id=user.id, mcp_id=instance.id)
    scope = a_turn(UserProfile(user_id=user.id))
    upstream, _ = an_upstream("answer")
    started, finish = asyncio.Event(), asyncio.Event()

    async def borrow() -> None:
        async with manager.acquire(scope, instance.namespace) as client:
            await client.list_tools()
            started.set()
            await finish.wait()

    async with connected(manager, upstream) as requests, asyncio.TaskGroup() as tasks:
        borrower = tasks.create_task(borrow())
        await asyncio.wait_for(started.wait(), timeout=5)
        try:
            await manager.disable(user_id=user.id, mcp_id=instance.id)
            await manager.enable(user_id=user.id, mcp_id=instance.id)
            async with manager.acquire(scope, instance.namespace) as replacement:
                cleanup = manager.sweaps[key]
                finish.set()
                await asyncio.wait_for(borrower, timeout=5)
                assert manager.sweaps[key] is cleanup
                assert not cleanup.done()
                await replacement.list_tools()
                assert requests.closed == 1
        finally:
            finish.set()
    assert requests.closed == 2
    assert manager.clients == manager.sweaps == {}


@pytest.mark.parametrize(
    "error_type",
    [
        httpx2.ConnectError,
        httpx2.ReadError,
        httpx2.WriteError,
        httpx2.ReadTimeout,
        httpx2.RemoteProtocolError,
    ],
)
async def test_fatal_network_failure_evicts_without_replaying_tool_call(
    manager: McpManager, error_type: type[httpx2.TransportError]
) -> None:
    user = await a_user()
    instance = await manager.install(user.id, install_request())
    key = McpClientKey(user_id=user.id, mcp_id=instance.id)
    scope = a_turn(UserProfile(user_id=user.id))
    upstream, calls = an_upstream("answer")
    async with connected(manager, upstream) as requests:
        transport = requests.upstream
        failed = False

        async def fail_once(request: httpx2.Request) -> httpx2.Response:
            nonlocal failed
            if not failed:
                failed = True
                if error_type is httpx2.ReadError:
                    # The server may have executed the tool before its reply is lost.
                    response = await transport.handle_async_request(request)
                    await response.aclose()
                raise error_type("upstream connection failed", request=request)
            return await transport.handle_async_request(request)

        requests.upstream = httpx2.MockTransport(fail_once)
        async with manager.acquire(scope, instance.namespace) as client:
            cleanup = manager.sweaps[key]
            with pytest.raises((MCPError, httpx2.TransportError)):
                await client.call_tool_mcp("answer", {})
        assert requests.methods.count("tools/call") == 1
        assert len(calls) == (1 if error_type is httpx2.ReadError else 0)
        assert manager.clients == manager.sweaps == {}
        assert cleanup.cancelled()
        assert requests.closed == 1
        async with manager.acquire(scope, instance.namespace) as replacement:
            assert replacement is not client
            await replacement.call_tool_mcp("answer", {})
        assert requests.methods.count("tools/call") == 2
    assert requests.closed == 2


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_http_error_preserves_usable_client(
    manager: McpManager, status: int
) -> None:
    user = await a_user()
    instance = await manager.install(user.id, install_request())
    key = McpClientKey(user_id=user.id, mcp_id=instance.id)
    scope = a_turn(UserProfile(user_id=user.id))
    upstream, _ = an_upstream("answer")
    async with connected(manager, upstream) as requests:
        transport = requests.upstream

        async def refuse(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(status, headers={"Retry-After": "0"})

        requests.upstream = httpx2.MockTransport(refuse)
        with pytest.raises(MCPError):
            async with manager.acquire(scope, instance.namespace) as client:
                await client.call_tool_mcp("answer", {})
        assert requests.methods.count("tools/call") == 1
        assert manager.clients[key] is client
        assert client.is_connected()
        assert requests.closed == 0
        requests.upstream = transport
        async with manager.acquire(scope, instance.namespace) as retained:
            assert retained is client
            await retained.call_tool_mcp("answer", {})
    assert requests.closed == 1


async def test_acquire_replaces_disconnected_client(manager: McpManager) -> None:
    user = await a_user()
    instance = await manager.install(user.id, install_request())
    scope = a_turn(UserProfile(user_id=user.id))
    upstream, _ = an_upstream("answer")
    async with connected(manager, upstream) as requests:
        async with manager.acquire(scope, instance.namespace) as client:
            await client.list_tools()
        await client.close()
        async with manager.acquire(scope, instance.namespace) as replacement:
            assert replacement is not client
            await replacement.list_tools()
    assert requests.closed == 2


async def test_old_failure_cannot_evict_replacement(manager: McpManager) -> None:
    user = await a_user()
    instance = await manager.install(user.id, install_request())
    key = McpClientKey(user_id=user.id, mcp_id=instance.id)
    scope = a_turn(UserProfile(user_id=user.id))
    upstream, _ = an_upstream("answer")
    started, finish = asyncio.Event(), asyncio.Event()

    async def borrow() -> None:
        async with manager.acquire(scope, instance.namespace):
            started.set()
            await finish.wait()
            raise httpx2.ReadError("late failure from old client")

    async with connected(manager, upstream) as requests:
        borrower = asyncio.create_task(borrow())
        await asyncio.wait_for(started.wait(), timeout=5)
        try:
            client = manager.clients[key]
            await client.close()
            async with manager.acquire(scope, instance.namespace) as replacement:
                assert replacement is not client
                finish.set()
                with pytest.raises(httpx2.ReadError, match="late failure"):
                    await asyncio.wait_for(borrower, timeout=5)
                assert manager.clients[key] is replacement
                await replacement.list_tools()
        finally:
            finish.set()
            if not borrower.done():
                borrower.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await borrower
    assert requests.closed == 2


async def test_cleanup_failure_does_not_hide_original_error(
    manager: McpManager,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = await a_user()
    instance = await manager.install(user.id, install_request())
    scope = a_turn(UserProfile(user_id=user.id))
    upstream, _ = an_upstream("answer")

    async def failed_close() -> None:
        raise RuntimeError("cleanup failed")

    async def fail() -> None:
        async with manager.acquire(scope, instance.namespace) as client:
            await client.close()
            monkeypatch.setattr(client, "close", failed_close)
            raise httpx2.ReadError("original call failed")

    async with connected(manager, upstream) as requests:
        with pytest.raises(httpx2.ReadError, match="original call failed"):
            await fail()
        assert manager.clients == manager.sweaps == {}
        assert "cleanup failed" in caplog.text
        async with manager.acquire(scope, instance.namespace) as replacement:
            await replacement.list_tools()
    assert requests.closed == 2
