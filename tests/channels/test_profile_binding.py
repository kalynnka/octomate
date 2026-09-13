from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import AnyHttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.config import AuthConfig, OAuthConfig, OctomateConfig
from octomate.database import async_session
from octomate.managers.gateway import OctomateSession
from octomate.mcp.oauth import LINK_PROFILE_TOOL
from octomate.mcp.server import tentacles_mcp
from octomate.schemas.auth import LinkProfileSession
from octomate.schemas.awakes import UserMessageSignal
from octomate.schemas.segments import TextSegment
from tests.support.channels import FakeChannelTentacle, FakeOctomate
from tests.support.managers import fixed_session


def link_profile_app() -> FakeOctomate:
    return FakeOctomate(
        config=OctomateConfig(
            auth=AuthConfig(
                access_token_salt=SecretStr("access-token-test-salt"),
                refresh_token_salt=SecretStr("refresh-token-test-salt"),
                api_key_salt=SecretStr("api-key-test-salt"),
            ),
            oauth=OAuthConfig(callback_base_uri=AnyHttpUrl("https://octomate.example")),
        )
    )


@pytest.fixture
async def link_profile_context(
    in_memory_engine: AsyncEngine,
) -> tuple[FakeChannelTentacle, OctomateSession, FastMCP]:
    app = link_profile_app()
    channel = FakeChannelTentacle(octomate=app)
    await channel.ingest(
        {
            "message_id": "link-1",
            "user_id": "U1",
            "chat_id": "D1",
            "chat_type": "dm",
            "segments": [TextSegment(data={"text": "Link my channel profile"})],
        }
    )
    [signal] = app.kicks
    assert isinstance(signal, UserMessageSignal)
    scope = OctomateSession(
        channel_routes={},
        current_agent_id="inkling",
        channels={channel.id: channel},
        user_profile=signal.messages[-1].sender,
        conversation_address=signal.address,
    )
    server = tentacles_mcp(fixed_session(scope), manager=app.mcp)
    return channel, scope, server


@pytest.mark.parametrize("shared", [False, True])
async def test_tool_delivers_only_to_the_resolved_users_private_surface(
    link_profile_context: tuple[FakeChannelTentacle, OctomateSession, FastMCP],
    shared: bool,
) -> None:
    channel, scope, server = link_profile_context
    assert scope.conversation_address is not None
    scope.conversation_address = replace(
        scope.conversation_address,
        shared=shared,
        chat_type="group" if shared else "dm",
        chat_id="C1" if shared else "D1",
    )
    async with Client(server) as client:
        tools = await client.list_tools()
        assert LINK_PROFILE_TOOL == "oauth_link_profile"
        assert not any(tool.name.startswith("profile_") for tool in tools)
        tool = next(tool for tool in tools if tool.name == LINK_PROFILE_TOOL)
        assert tool.input_schema.get("properties", {}) == {}
        result = await client.call_tool(LINK_PROFILE_TOOL, {})

    [sent] = channel.sent
    assert sent[0] == ("U1" if shared else "D1")
    assert sent[1] == "dm"
    assert channel.opened_dms == (["U1"] if shared else [])
    url = sent[2][0]["text"].split("](", 1)[1].split(")", 1)[0]
    [ticket] = parse_qs(urlsplit(url).fragment)["link-profile"]
    assert ticket not in str(result)
    assert "link-profile=" not in str(result)
    assert "sent privately" in str(result)
    assert scope.user_profile is not None
    info = await channel.octomate.users.inspect_link_profile(SecretStr(ticket))
    assert info.profile.id == scope.user_profile.id
    assert info.profile.channel_user_id == "U1"


@pytest.mark.parametrize("mismatch", ["user", "channel", "missing"])
async def test_tool_refuses_a_missing_or_mismatched_turn_identity(
    link_profile_context: tuple[FakeChannelTentacle, OctomateSession, FastMCP],
    mismatch: str,
) -> None:
    channel, scope, server = link_profile_context
    assert scope.conversation_address is not None
    if mismatch == "missing":
        scope.user_profile = None
    elif mismatch == "user":
        scope.conversation_address = replace(scope.conversation_address, user_id="U2")
    else:
        scope.channels["other"] = channel
        scope.conversation_address = replace(
            scope.conversation_address, channel_tentacle_id="other"
        )
    async with Client(server) as client:
        with pytest.raises(ToolError, match=r"requires a turn|does not match"):
            await client.call_tool(LINK_PROFILE_TOOL, {})
    assert channel.sent == []
    assert channel.opened_dms == []
    async with async_session() as session:
        assert await session.list(LinkProfileSession) == []


async def test_no_private_surface_refuses_without_creating_a_ticket(
    link_profile_context: tuple[FakeChannelTentacle, OctomateSession, FastMCP],
) -> None:
    channel, scope, server = link_profile_context
    assert scope.conversation_address is not None
    scope.conversation_address = replace(
        scope.conversation_address, shared=True, chat_type="group", chat_id="C1"
    )
    channel.recording_ink.dm_opens = False
    async with Client(server) as client:
        with pytest.raises(ToolError, match="no private surface"):
            await client.call_tool(LINK_PROFILE_TOOL, {})
    assert channel.opened_dms == ["U1"]
    assert channel.sent == []
    async with async_session() as session:
        assert await session.list(LinkProfileSession) == []


async def test_failed_private_delivery_is_a_tool_error(
    link_profile_context: tuple[FakeChannelTentacle, OctomateSession, FastMCP],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel, _scope, server = link_profile_context
    send = AsyncMock(return_value=None)
    monkeypatch.setattr(channel.recording_ink, "send_message", send)
    async with Client(server) as client:
        with pytest.raises(ToolError, match="could not deliver") as refused:
            await client.call_tool(LINK_PROFILE_TOOL, {})
    send.assert_awaited_once()
    assert channel.sent == []
    assert "link-profile=" not in str(refused.value)


async def test_bind_command_is_ordinary_agent_input(
    in_memory_engine: AsyncEngine,
) -> None:
    app = link_profile_app()
    channel = FakeChannelTentacle(octomate=app)

    await channel.ingest(
        {
            "message_id": "link-1",
            "user_id": "U1",
            "chat_id": "D1",
            "chat_type": "dm",
            "segments": [TextSegment(data={"text": "/bind"})],
        }
    )

    [signal] = app.kicks
    assert isinstance(signal, UserMessageSignal)
    assert signal.messages[-1].segments == [TextSegment(data={"text": "/bind"})]
    assert channel.sent == []
    async with async_session() as session:
        assert await session.list(LinkProfileSession) == []
