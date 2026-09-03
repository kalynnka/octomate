"""Driven Claude's gateway: the same server, walked into the SDK's tool shape.

The handlers close over one turn's `GatewaySession`, so what these tests pin is
the translation — the spell runs against the session, the sentence comes back as
the tool's text, and a refusal arrives as an `is_error` result carrying Inkling's
wording verbatim, which is how Claude retries from it natively.
"""

from __future__ import annotations

from claude_agent_sdk import SdkMcpTool
from fastmcp.dependencies import Depends
from pydantic import JsonValue

from octomate.managers.gateway import GatewaySession
from octomate.mcp.gateway import TELEPORT_RECORDED
from octomate.mcp.server import octomate_mcp
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import (
    AgentRoute,
    Claim,
    SummonDecision,
    ThreadLanding,
)
from octomate.tentacles.agents.claude.gateway import (
    GATEWAY_MCP_INSTRUCTION,
    gateway_mcp_server,
    sdk_gateway_tool,
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


def a_turn() -> GatewaySession:
    return GatewaySession(
        channel_routes={"im": [CLAUDE_ROUTE]},
        current_agent_id="inkling",
        channels={"im": FakeChannelTentacle()},
        conversation_address=ChannelAddress(
            channel_tentacle_id="im",
            chat_type="group",
            chat_id="room",
            user_id="alice",
            shared=True,
        ),
    )


async def spells(
    session: GatewaySession,
) -> dict[str, SdkMcpTool[dict[str, JsonValue]]]:
    server = octomate_mcp(Depends(lambda: session), FakeThreadManager())
    return {tool.name: sdk_gateway_tool(tool) for tool in await server.list_tools()}


def the_text(result: dict[str, JsonValue]) -> str:
    content = result["content"]
    assert isinstance(content, list)
    [block] = content
    assert isinstance(block, dict)
    text = block["text"]
    assert isinstance(text, str)
    return text


async def test_the_server_config_is_the_sdk_in_process_shape() -> None:
    config = await gateway_mcp_server(a_turn(), FakeThreadManager())

    assert config["type"] == "sdk"
    assert config["name"] == "gateway"


def test_the_instruction_names_the_tools_the_claude_way() -> None:
    for name in (
        "scry",
        "summon",
        "teleport",
        "scheme",
        "send",
        "search_thread_history",
    ):
        assert f"`mcp__gateway__{name}`" in GATEWAY_MCP_INSTRUCTION
    assert "{" not in GATEWAY_MCP_INSTRUCTION
    assert "commission" not in GATEWAY_MCP_INSTRUCTION


async def test_summon_records_the_decision_and_answers_with_the_sentence() -> None:
    session = a_turn()
    tools = await spells(session)

    result = await tools["summon"].handler(dict(SUMMON_ARGUMENTS))

    assert the_text(result) == "Summoning claude (opus) → thread."
    assert "is_error" not in result
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


async def test_a_refusal_is_an_error_result_carrying_the_same_sentence() -> None:
    session = a_turn()
    tools = await spells(session)

    result = await tools["summon"].handler({**SUMMON_ARGUMENTS, "agent_id": "nobody"})

    # Verbatim — the sentence Inkling's `ModelRetry` carries.
    assert the_text(result).startswith("Invalid summon route (agent_id='nobody'")
    assert result["is_error"] is True
    assert session.decision is None


async def test_arguments_are_validated_before_policy_runs() -> None:
    session = a_turn()
    tools = await spells(session)

    # The bad destination kind is the input under test: schema validation refuses
    # it before any policy is consulted, and the refusal is a retryable error.
    result = await tools["summon"].handler(
        {**SUMMON_ARGUMENTS, "destination": {"kind": "everywhere"}}
    )

    assert result["is_error"] is True
    assert "destination" in the_text(result)
    assert session.decision is None


async def test_teleport_tells_the_runtime_to_wrap_up() -> None:
    session = a_turn()
    tools = await spells(session)

    result = await tools["teleport"].handler({"hint": "carrying on in a thread"})

    assert the_text(result) == TELEPORT_RECORDED
    assert session.decision is not None
