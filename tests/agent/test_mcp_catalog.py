from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError
from pydantic import AnyHttpUrl, SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config.mcp import BareMcpConfig
from octomate.managers.mcp import McpUnavailable
from octomate.mcp.server import CALL_MCP_TOOL, LIST_MCPS, tentacles_mcp
from octomate.schemas.mcp import BearerMcp, McpInstallRequest, NoAuthMcp
from octomate.tentacles.mcp import BareMcpTentacle, build_mcp
from tests.agent.test_mcp import ENCRYPTION_KEY, a_turn, an_upstream
from tests.managers.test_mcp import connected
from tests.support.managers import fixed_session
from tests.support.mcp import discover
from tests.support.users import a_user


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


def request(namespace: str = "research") -> McpInstallRequest:
    return McpInstallRequest(
        name="Research",
        namespace=namespace,
        url=AnyHttpUrl("https://mcp.example/mcp"),
        tentacle_id="provider",
    )


@pytest.mark.parametrize(
    "auth",
    [
        {"kind": "none"},
        {"kind": "bearer", "token": "user-secret"},
        {"kind": "oauth"},
    ],
)
def test_install_cannot_override_tentacle_auth(auth: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="tentacle supplies its own"):
        McpInstallRequest.model_validate(request().model_dump() | {"auth": auth})


async def test_available_tentacles_do_not_install_for_users() -> None:
    host = Octomate()
    owner = await a_user("alice")
    tentacle = BareMcpTentacle(
        "provider", host, url="https://mcp.example/mcp", token=SecretStr("operator")
    )
    host.connect(tentacle)
    assert [entry.id for entry in host.mcp.available()] == ["provider"]
    assert await host.mcp.list(owner.id) == []
    assert "operator" not in (host.mcp.available())[0].model_dump_json()
    assert host.mcp.available()[0] is tentacle.info
    host.mcp.tentacles.clear()
    assert host.mcp.available() == []
    host.mcp.tentacles[tentacle.id] = tentacle
    assert len(host.mcp.available()) == 1


@pytest.mark.parametrize("token", [None, SecretStr("operator")])
async def test_tentacle_requires_explicit_install_and_keeps_users_isolated(
    token: SecretStr | None,
) -> None:
    host = Octomate(oauth_encryption_key=ENCRYPTION_KEY)
    alice = await a_user("alice", profiles={"slack": "U1"})
    bob = await a_user("bob", profiles={"slack": "U2"})
    profiles = [await host.users.profile("slack", id) for id in ("U1", "U2")]
    tentacle = build_mcp(
        "provider", BareMcpConfig(url="https://mcp.example/mcp", token=token), host
    )
    host.mcp.tentacles[tentacle.id] = tentacle
    scopes = [a_turn(profile) for profile in profiles]
    tentacle.instructions = "Use the connected workspace for every request."
    servers = [
        tentacles_mcp(fixed_session(scope), manager=host.mcp) for scope in scopes
    ]
    upstream, seen = an_upstream("answer")
    async with connected(host.mcp, upstream):
        for server in servers:
            with pytest.raises(ToolError, match="unavailable"):
                await discover(server, "provider")
            assert (await server.call_tool(LIST_MCPS, {})).structured_content == {
                "result": []
            }
        installed = await host.mcp.install(alice.id, request())
        assert installed.tentacle_id == tentacle.id
        if token is None:
            assert isinstance(installed, NoAuthMcp)
        else:
            assert isinstance(installed, BearerMcp)
            assert installed.encrypted_token
            assert b"operator" not in installed.encrypted_token
        assert installed.auth_kind == host.mcp.available()[0].auth_kind
        assert (
            tentacle.instructions
            in (await discover(servers[0], installed.namespace)).instructions
        )
        assert [
            tool.name
            for tool in (await discover(servers[0], installed.namespace)).tools
        ] == ["answer"]
        await servers[0].call_tool(
            CALL_MCP_TOOL,
            {"namespace": installed.namespace, "name": "answer", "arguments": {}},
        )
        with pytest.raises(ToolError, match="unavailable"):
            await discover(servers[1], installed.namespace)
        assert await host.mcp.list(bob.id) == []
        await host.mcp.disable(alice.id, installed.id)
        host.mcp.tentacles[tentacle.id] = tentacle
        assert not (await host.mcp.list(alice.id))[0].enabled
        await host.mcp.enable(alice.id, installed.id)
        tentacle.upstream = "https://changed.example/mcp"
        tentacle.instructions = "Changed instructions"
        tentacle.token = SecretStr("changed-token")
        host.mcp.tentacles.clear()
        await host.mcp.disable(alice.id, installed.id)
        await host.mcp.enable(alice.id, installed.id)
        catalog = await discover(servers[0], installed.namespace)
        assert catalog.instructions == installed.instructions
        await servers[0].call_tool(
            CALL_MCP_TOOL,
            {"namespace": installed.namespace, "name": "answer", "arguments": {}},
        )
        host.mcp.tentacles[tentacle.id] = tentacle
        await host.mcp.uninstall(alice.id, installed.id)
        host.mcp.tentacles[tentacle.id] = tentacle
        assert await host.mcp.list(alice.id) == []
    assert seen == ["Bearer operator" if token is not None else ""] * 2


async def test_install_refuses_missing_or_changed_tentacle() -> None:
    host = Octomate()
    owner = await a_user("alice")
    with pytest.raises(McpUnavailable):
        await host.mcp.install(owner.id, request())
    tentacle = BareMcpTentacle(
        "provider", host, url="https://other.example/mcp", token=SecretStr("operator")
    )
    host.mcp.tentacles[tentacle.id] = tentacle
    with pytest.raises(ValueError, match="URL"):
        await host.mcp.install(owner.id, request())
    host.mcp.tentacles.clear()
    with pytest.raises(McpUnavailable):
        await host.mcp.install(owner.id, request())
