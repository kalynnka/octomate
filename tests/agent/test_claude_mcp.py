"""Driven Claude's MCP server: the same served server, walked into the SDK's tool
shape.

The handlers close over one turn's `OctomateSession`, so what these tests pin is
the translation — the spell runs against the session, the sentence comes back as
the tool's text, and a refusal arrives as an `is_error` result carrying Inkling's
wording verbatim, which is how Claude retries from it natively.
"""

from __future__ import annotations

from claude_agent_sdk import SdkMcpTool
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import ImageContent
from pydantic import JsonValue, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.managers.gateway import OctomateSession
from octomate.mcp.gateway import TELEPORT_RECORDED
from octomate.mcp.server import (
    CALL_MCP_TOOL,
    LIST_MCP_TOOLS,
    McpToolCatalog,
    octomate_instructions,
    octomate_mcp,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import (
    AgentRoute,
    Claim,
    SummonDecision,
    ThreadLanding,
)
from octomate.tentacles.claude.mcp import octomate_mcp_server, sdk_tool
from octomate.tentacles.mcp import BareMcpTentacle
from tests.agent.test_mcp import ENCRYPTION_KEY, an_upstream, upstream_of
from tests.channels.slack.test_mcp import into
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import FakeThreadManager, fixed_session
from tests.support.mcp import install_tentacle
from tests.support.users import a_user

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


def a_turn() -> OctomateSession:
    return OctomateSession(
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
    session: OctomateSession,
) -> dict[str, SdkMcpTool[dict[str, JsonValue]]]:
    server = octomate_mcp(
        fixed_session(session), FakeThreadManager(), manager=Octomate().mcp
    )
    return {tool.name: sdk_tool(tool) for tool in await server.list_tools()}


def the_text(result: dict[str, JsonValue]) -> str:
    content = result["content"]
    assert isinstance(content, list)
    [block] = content
    assert isinstance(block, dict)
    text = block["text"]
    assert isinstance(text, str)
    return text


async def test_the_server_config_is_the_sdk_in_process_shape() -> None:
    config = await octomate_mcp_server(
        a_turn(), FakeThreadManager(), manager=Octomate().mcp
    )

    assert config["type"] == "sdk"
    assert config["name"] == "octomate"


async def test_image_content_keeps_the_mcp_wire_field_names() -> None:
    server = FastMCP("images")

    @server.tool(description="Return a picture.")
    def picture() -> ToolResult:
        return ToolResult(
            content=[ImageContent(type="image", data="aW1hZ2U=", mime_type="image/png")]
        )

    picture_tool = await server.get_tool("picture")
    assert picture_tool is not None
    tool = sdk_tool(picture_tool)
    result = await tool.handler({})

    assert result == {
        "content": [{"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"}]
    }


def test_the_instruction_names_the_tools_by_their_served_names() -> None:
    # Claude lists them as `mcp__octomate__<tool>` and resolves that itself, as
    # it does for every MCP server's instructions.
    instruction = octomate_instructions()

    for name in (
        "gateway_scry",
        "gateway_summon",
        "gateway_teleport",
        "gateway_scheme",
        "gateway_send",
        "gateway_dispel",
        "history_search",
    ):
        assert f"`{name}`" in instruction
    assert "mcp__" not in instruction
    assert "{" not in instruction
    assert "commission" not in instruction


async def test_summon_records_the_decision_and_answers_with_the_sentence() -> None:
    session = a_turn()
    tools = await spells(session)

    result = await tools["gateway_summon"].handler(dict(SUMMON_ARGUMENTS))

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

    result = await tools["gateway_summon"].handler(
        {**SUMMON_ARGUMENTS, "agent_id": "nobody"}
    )

    # Verbatim — the sentence Inkling's `ModelRetry` carries.
    assert the_text(result).startswith("Invalid summon route (agent_id='nobody'")
    assert result["is_error"] is True
    assert session.decision is None


async def test_arguments_are_validated_before_policy_runs() -> None:
    session = a_turn()
    tools = await spells(session)

    # The bad destination kind is the input under test: schema validation refuses
    # it before any policy is consulted, and the refusal is a retryable error.
    result = await tools["gateway_summon"].handler(
        {**SUMMON_ARGUMENTS, "destination": {"kind": "everywhere"}}
    )

    assert result["is_error"] is True
    assert "destination" in the_text(result)
    assert session.decision is None


async def test_teleport_tells_the_runtime_to_wrap_up() -> None:
    session = a_turn()
    tools = await spells(session)

    result = await tools["gateway_teleport"].handler(
        {"hint": "carrying on in a thread"}
    )

    assert the_text(result) == TELEPORT_RECORDED
    assert session.decision is not None


async def test_sdk_discovers_and_calls_provider_tools_through_fixed_helpers(
    in_memory_engine: AsyncEngine,
) -> None:
    tentacle = BareMcpTentacle(
        "provider",
        Octomate(oauth_encryption_key=ENCRYPTION_KEY),
        url="https://mcp.example/mcp",
        token=SecretStr("key"),
    )
    upstream, calls = an_upstream("answer")

    @upstream.tool
    def refuse() -> str:
        """A tool that refuses the request."""
        raise ToolError("Provider refused the request")

    await a_user("alice", profiles={"slack": "U1"})
    profile = await tentacle.octomate.users.profile("slack", "U1")
    assert profile is not None
    await install_tentacle(tentacle, profile)
    session = a_turn()
    session.user_profile = profile
    async with upstream_of(upstream) as transport, tentacle.octomate.mcp.lifespan():
        tentacle.octomate.mcp.httpx_client_factory = into(transport)
        server = octomate_mcp(
            fixed_session(session),
            FakeThreadManager(),
            manager=tentacle.octomate.mcp,
        )
        tools = {tool.name: sdk_tool(tool) for tool in await server.list_tools()}
        assert "answer" not in tools
        assert calls == []
        discovered = await tools[LIST_MCP_TOOLS].handler(
            {"namespace": "personal/provider"}
        )
        catalog = McpToolCatalog.model_validate_json(the_text(discovered))
        assert {tool.name for tool in catalog.tools} == {
            "answer",
            "refuse",
        }
        answered = await tools[CALL_MCP_TOOL].handler(
            {"namespace": "personal/provider", "name": "answer", "arguments": {}}
        )
        refused = await tools[CALL_MCP_TOOL].handler(
            {"namespace": "personal/provider", "name": "refuse", "arguments": {}}
        )

    assert the_text(answered) == "answered"
    assert calls == ["Bearer key"]
    assert refused["is_error"] is True
    assert "Provider refused the request" in the_text(refused)
