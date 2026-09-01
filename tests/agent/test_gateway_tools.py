"""The gateway's MCP server: one contract, one policy, several runtimes.

What every runtime other than Inkling calls must be exactly what Inkling's toolset
compiles — same descriptions, same refusal sentences, same schema discipline — so
these tests pin the server to the capability rather than to copies of its strings.
They speak to it in memory, which is how a driven Claude run reaches it too.
"""

from __future__ import annotations

import json
from inspect import cleandoc

import pytest
from fastmcp import Client, FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.capabilities.gateway import GatewayCapability, gateway_instructions
from octomate.config.users import UserConfig
from octomate.managers.gateway import GatewaySession
from octomate.managers.user import UserManager
from octomate.mcp.gateway import GATEWAY_SPELLS, TELEPORT_RECORDED, gateway_mcp
from octomate.schemas.awakes import GatewayHandoffSignal
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MarkdownSegment
from octomate.schemas.triage import (
    AgentRoute,
    Claim,
    CrossingLanding,
    SchemeDecision,
    SummonDecision,
    TeleportDecision,
    ThreadLanding,
)
from octomate.types.threads import CLAUDE_NATIVE_ID
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import FakeThreadManager

CLAUDE_ROUTE = AgentRoute(
    agent_id="claude",
    model="opus",
    claim=Claim(ability="coding work", efforts=("medium", "high")),
)

SUMMON_ARGUMENTS = {
    "agent_id": "claude",
    "model": "opus",
    "destination": {"kind": "thread"},
    "hint": "Working on it",
    "reason": "needs coding",
    "summon": "Please investigate the failing test.",
}


def a_turn() -> tuple[FastMCP, GatewaySession, FakeChannelTentacle, FakeThreadManager]:
    """The gateway as a driven runtime mounts it for one turn on a group main: every
    call against one fixed session, delivering through the fakes handed back."""
    channel = FakeChannelTentacle()
    threads = FakeThreadManager()
    session = GatewaySession(
        channel_routes={"im": [CLAUDE_ROUTE]},
        current_agent_id="inkling",
        channels={"im": channel},
        conversation_address=ChannelAddress(
            channel_tentacle_id="im",
            chat_type="group",
            chat_id="room",
            user_id="alice",
            shared=True,
        ),
    )
    return gateway_mcp(Depends(lambda: session), threads), session, channel, threads


async def a_native_call(
    *, linked: bool = True
) -> tuple[
    FastMCP,
    GatewaySession,
    FakeChannelTentacle,
    FakeThreadManager,
    list[GatewayHandoffSignal],
]:
    """The gateway as the served endpoint builds it for one native call: no
    thread, no address, the session speaking for the registered user the
    verified bearer named — `linked` is whether that user has a real account
    on `im` for a destination to light up."""
    channel = FakeChannelTentacle()
    threads = FakeThreadManager()
    kicks: list[GatewayHandoffSignal] = []
    users = UserManager(
        {
            "luhui": UserConfig.model_validate(
                {
                    "secret": "luhui-token",
                    "profiles": {"im": {"channel_user_id": "alice"}},
                }
                if linked
                else {"secret": "luhui-token"}
            )
        }
    )
    await users.reconcile()
    session = GatewaySession(
        channel_routes={"im": [CLAUDE_ROUTE]},
        current_agent_id=CLAUDE_NATIVE_ID,
        channels={"im": channel},
        users=users,
        user_profile=await users.native_profile(CLAUDE_NATIVE_ID, "luhui"),
        native=True,
    )
    server = gateway_mcp(Depends(lambda: session), threads, kick=kicks.append)
    return server, session, channel, threads, kicks


async def test_the_server_offers_exactly_the_six_shared_spells() -> None:
    # The accomplice spells are deliberately absent: external runtimes bring
    # their own subagent systems.
    server, _session, _channel, _threads = a_turn()

    tools = await server.list_tools()

    assert [tool.name for tool in tools] == [
        "scry",
        "summon",
        "teleport",
        "scheme",
        "send",
        "bind",
    ]
    # What an adapter pre-allows is this same list, named statically.
    assert list(GATEWAY_SPELLS) == [tool.name for tool in tools]


async def test_descriptions_are_the_inkling_contracts_verbatim() -> None:
    server, _session, _channel, _threads = a_turn()
    contracts = {
        "scry": GatewayCapability.scry,
        "summon": GatewayCapability.summon,
        "teleport": GatewayCapability.teleport,
        "scheme": GatewayCapability.scheme,
        "send": GatewayCapability.send,
        "bind": GatewayCapability.bind,
    }

    tools = {tool.name: tool for tool in await server.list_tools()}

    for name, spell in contracts.items():
        doc = spell.__doc__
        assert doc is not None
        assert tools[name].description == cleandoc(doc)
    # And the contract is the model-facing one, not a paraphrase.
    assert "copied exactly from a `scry` route" in (tools["summon"].description or "")
    assert "do NOT repeat it in your final reply" in (tools["send"].description or "")


def test_gateway_instructions_render_one_contract_under_each_naming() -> None:
    session = GatewaySession(channel_routes={}, current_agent_id="inkling")
    inkling = GatewayCapability(session=session).get_instructions()
    assert gateway_instructions(lambda name: name) == inkling

    mcp = gateway_instructions(lambda name: f"mcp__gateway__{name}")
    for name in ("scry", "summon", "teleport", "scheme", "send", "bind"):
        assert f"`mcp__gateway__{name}`" in mcp
    assert "{" not in mcp
    assert "commission" not in mcp


async def test_schemas_carry_no_runtime_state() -> None:
    # The same discipline as the Inkling toolset: tool definitions are cached
    # prompt segments, so nothing from any particular run may reach them — not
    # the session, which is injected, and not what it knows.
    server, _session, _channel, _threads = a_turn()

    for tool in await server.list_tools():
        rendered = json.dumps(tool.parameters)
        assert "session" not in tool.parameters.get("properties", {})
        for runtime_state in ("im", "alice", "room", "claude"):
            assert f'"{runtime_state}"' not in rendered


async def test_summon_records_the_decision_it_validated() -> None:
    server, session, _channel, _threads = a_turn()

    async with Client(server) as client:
        result = await client.call_tool("summon", SUMMON_ARGUMENTS)

    assert result.data == "Summoning claude (opus) → thread."
    assert session.decision == SummonDecision(
        action="summon",
        agent_id="claude",
        model="opus",
        destination=ThreadLanding(),
        effort=None,
        hint="Working on it",
        reason="needs coding",
        summon="Please investigate the failing test.",
    )


async def test_a_refusal_reaches_the_caller_as_the_same_sentence() -> None:
    server, session, _channel, _threads = a_turn()

    async with Client(server) as client:
        with pytest.raises(ToolError) as refusal:
            await client.call_tool("summon", {**SUMMON_ARGUMENTS, "agent_id": "nobody"})

    # Verbatim — the sentence Inkling's `ModelRetry` carries, with no wrapper
    # prose around it.
    assert str(refusal.value).startswith("Invalid summon route (agent_id='nobody'")
    assert session.decision is None


async def test_arguments_are_validated_before_policy_runs() -> None:
    server, session, _channel, _threads = a_turn()

    async with Client(server) as client:
        # The bad destination kind is the input under test: the server validates
        # against the tool's own schema before any policy is consulted.
        with pytest.raises(ToolError, match="destination"):
            await client.call_tool(
                "summon", {**SUMMON_ARGUMENTS, "destination": {"kind": "everywhere"}}
            )

    assert session.decision is None


async def test_teleport_records_and_tells_the_runtime_to_wrap_up() -> None:
    server, session, _channel, _threads = a_turn()

    async with Client(server) as client:
        result = await client.call_tool("teleport", {"hint": "carrying on in a thread"})

    assert result.data == TELEPORT_RECORDED
    assert isinstance(session.decision, TeleportDecision)
    assert session.decision.hint == "carrying on in a thread"
    assert session.decision.crossing is None


async def test_send_here_delivers_immediately_to_the_conversation() -> None:
    server, _session, channel, threads = a_turn()

    async with Client(server) as client:
        result = await client.call_tool(
            "send",
            {"segments": [{"type": "markdown", "data": {"text": "halfway there"}}]},
        )

    assert result.data == "sent"
    assert channel.recording_ink.sent
    [outbound] = threads.outbounds
    assert outbound.agent_tentacle_id == "inkling"
    assert outbound.segments == [MarkdownSegment(data={"text": "halfway there"})]


async def test_send_to_dm_opens_it_and_lands_there() -> None:
    server, _session, channel, threads = a_turn()

    async with Client(server) as client:
        result = await client.call_tool(
            "send",
            {
                "segments": [{"type": "markdown", "data": {"text": "the summary"}}],
                "destination": {"kind": "dm"},
            },
        )

    assert result.data == "sent"
    assert channel.opened_dms == ["alice"]
    [outbound] = threads.outbounds
    assert outbound.direction == "outbound"


async def test_a_native_session_scries_only_crossings(
    in_memory_engine: AsyncEngine,
) -> None:
    server, session, _channel, _threads, _kicks = await a_native_call()

    async with Client(server) as client:
        routes = await client.call_tool("scry", {"reveal": "routes"})
        places = await client.call_tool("scry", {"reveal": "destinations"})

    # No conversation of its own: nothing to route to here, and neither built-in
    # landing exists — everywhere it can go is the linked account's crossing.
    assert routes.data == "- (none)"
    assert "their direct messages on" in places.data
    assert await session.summon_handles() == ["im"]


async def test_a_native_session_with_no_linked_accounts_scries_nowhere(
    in_memory_engine: AsyncEngine,
) -> None:
    server, session, _channel, _threads, kicks = await a_native_call(linked=False)

    async with Client(server) as client:
        result = await client.call_tool("scry", {"reveal": "destinations"})
        with pytest.raises(ToolError) as refusal:
            await client.call_tool("summon", SUMMON_ARGUMENTS)

    # Registered, so the session knows who it speaks for — but the user has no
    # account anywhere a destination could light up.
    assert session.user_profile is not None
    assert result.data == "- (none)"
    # The truthful dead end: no linked account, so nowhere left to land.
    assert "`summon` has nowhere left to land, so answer it." in str(refusal.value)
    assert kicks == []


async def test_a_native_teleport_is_refused_honestly(
    in_memory_engine: AsyncEngine,
) -> None:
    server, _session, _channel, _threads, kicks = await a_native_call()

    async with Client(server) as client:
        with pytest.raises(ToolError) as refusal:
            await client.call_tool("teleport", {"hint": "moving over"})

    assert str(refusal.value).startswith(
        "This session lives in your terminal — Octomate cannot relocate it."
    )
    assert kicks == []


async def test_a_native_send_here_is_refused(
    in_memory_engine: AsyncEngine,
) -> None:
    server, _session, _channel, threads, _kicks = await a_native_call()

    async with Client(server) as client:
        with pytest.raises(ToolError) as refusal:
            await client.call_tool(
                "send",
                {"segments": [{"type": "markdown", "data": {"text": "hello"}}]},
            )

    assert str(refusal.value) == (
        "This session has no conversation of its own to land a send on — "
        'name a destination from `scry` (`reveal="destinations"`).'
    )
    assert threads.outbounds == []


async def test_a_native_send_to_dm_is_refused(
    in_memory_engine: AsyncEngine,
) -> None:
    server, _session, _channel, threads, _kicks = await a_native_call()

    async with Client(server) as client:
        with pytest.raises(ToolError) as refusal:
            await client.call_tool(
                "send",
                {
                    "segments": [{"type": "markdown", "data": {"text": "hello"}}],
                    "destination": {"kind": "dm"},
                },
            )

    assert str(refusal.value).startswith(
        "This run has no single user whose direct messages could be opened."
    )
    assert threads.outbounds == []


async def test_a_native_send_delivers_to_a_crossing(
    in_memory_engine: AsyncEngine,
) -> None:
    server, _session, channel, threads, kicks = await a_native_call()

    async with Client(server) as client:
        result = await client.call_tool(
            "send",
            {
                "segments": [{"type": "markdown", "data": {"text": "for you"}}],
                "destination": {"kind": "channel", "channel": "im"},
            },
        )

    assert result.data == "sent"
    assert channel.opened_dms == ["alice"]
    [outbound] = threads.outbounds
    # The ledger row is attributed to the native runtime, never to a session.
    assert outbound.agent_tentacle_id == CLAUDE_NATIVE_ID
    assert kicks == []


async def test_a_native_summon_kicks_its_handoff_at_once(
    in_memory_engine: AsyncEngine,
) -> None:
    server, session, _channel, _threads, kicks = await a_native_call()

    async with Client(server) as client:
        result = await client.call_tool(
            "summon",
            {**SUMMON_ARGUMENTS, "destination": {"kind": "channel", "channel": "im"}},
        )

    assert result.data == "Summoning claude (opus) → im."
    [signal] = kicks
    assert signal.agent_id == CLAUDE_NATIVE_ID
    assert signal.user_profile is session.user_profile
    assert signal.source is not None
    assert signal.source.channel_tentacle_id == CLAUDE_NATIVE_ID
    assert isinstance(signal.decision, SummonDecision)
    assert signal.decision.summon == "Please investigate the failing test."
    assert signal.decision.destination == CrossingLanding(
        address=ChannelAddress(
            channel_tentacle_id="im", chat_type="dm", chat_id="", user_id="alice"
        )
    )


async def test_a_native_scheme_kicks_its_handoff_at_once(
    in_memory_engine: AsyncEngine,
) -> None:
    server, _session, _channel, _threads, kicks = await a_native_call()

    async with Client(server) as client:
        result = await client.call_tool(
            "scheme",
            {
                "hint": "carrying on with you directly",
                "brief": "The operator asked for a summary.",
                "destination": {"kind": "channel", "channel": "im"},
            },
        )

    assert result.data.startswith("Taking this to")
    [signal] = kicks
    assert signal.agent_id == CLAUDE_NATIVE_ID
    assert isinstance(signal.decision, SchemeDecision)
    assert signal.decision.brief == "The operator asked for a summary."
    assert signal.decision.destination.user_id == "alice"
