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

from octomate.capabilities.gateway import GatewayCapability, gateway_instructions
from octomate.managers.gateway import GatewaySession
from octomate.mcp.gateway import GATEWAY_SPELLS, TELEPORT_RECORDED, gateway_mcp
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MarkdownSegment
from octomate.schemas.triage import (
    AgentRoute,
    Claim,
    SummonDecision,
    TeleportDecision,
    ThreadLanding,
)
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


async def test_the_server_offers_exactly_the_five_shared_spells() -> None:
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
    for name in ("scry", "summon", "teleport", "scheme", "send"):
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
