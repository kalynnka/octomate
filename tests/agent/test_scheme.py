"""The gate's scheme spells: an ordinary awaited tool call that runs another
agent in its own subagent conversation and returns the report — plus the guards
that keep an accomplice an accomplice (no gate of its own, bounded time, loud
failure instead of a parked deferral)."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests

from octomate.capabilities.gateway import (
    SCHEME_TOOL_NAME,
    WHISPER_TOOL_NAME,
    ACCOMPLICE_INSTRUCTION,
    GatewayCapability,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import AgentRoute, Claim
from octomate.tentacles.agent.base import AgentTentacle
from tests.support.agents import FakeAgent
from tests.support.managers import FakeConversationManager

THREAD = uuid.uuid4()
ADDRESS = ChannelAddress(
    channel_tentacle_id="im",
    chat_type="private",
    chat_id="alice",
    user_id="alice",
    thread_id="",
)
CLAUDE_ROUTE = AgentRoute(
    agent_id="claude",
    model="opus",
    claim=Claim(ability="coding work", efforts=("low", "medium", "high")),
)


def _ctx(
    parent_id: uuid.UUID,
    run_id: str = "run-parent",
    tool_call_id: str = "call-1",
) -> RunContext[None]:
    # What a live run's RunContext carries: the run id, the tool call, and the
    # calling run's own conversation id.
    return cast(
        RunContext[None],
        SimpleNamespace(
            run_id=run_id,
            tool_call_id=tool_call_id,
            conversation_id=str(parent_id),
        ),
    )


async def _gate(
    *,
    agents: dict[str, AgentTentacle] | None = None,
    conversations: FakeConversationManager | None = None,
    scheme_timeout: float = 5.0,
) -> tuple[
    GatewayCapability,
    dict[str, AgentTentacle],
    FakeConversationManager,
    RunContext[None],
]:
    agents = agents or {
        "inkling": cast(AgentTentacle, FakeAgent(id="inkling")),
        "claude": cast(
            AgentTentacle, FakeAgent(id="claude", allow_reception_run=True)
        ),
    }
    conversations = conversations or FakeConversationManager()
    gate = GatewayCapability(
        routes=[CLAUDE_ROUTE],
        current_agent_id="inkling",
        agents=agents,
        conversations=conversations,
        thread_id=THREAD,
        conversation_address=ADDRESS,
        scheme_timeout=scheme_timeout,
    )
    parent = await conversations.ensure(THREAD, agent_tentacle_id="inkling")
    return gate, agents, conversations, _ctx(parent.id)


def _tool(gate: GatewayCapability, name: str):
    assert gate.toolset is not None
    return gate.toolset.tools[name].function


async def test_scheme_runs_the_accomplice_and_returns_its_report() -> None:
    gate, agents, conversations, ctx = await _gate()
    claude = cast(FakeAgent, agents["claude"])
    claude.reception_output = "audit: three findings"

    report = await _tool(gate, SCHEME_TOOL_NAME)(
        ctx,
        name="repo-audit",
        agent_id="claude",
        model="opus",
        brief="Audit the repo.",
    )

    assert report == "audit: three findings"
    turn = claude.turns[0]
    assert turn.prompt == "Audit the repo."
    assert turn.run_name == "scheme"
    assert turn.thread_id == THREAD and turn.address == ADDRESS
    assert turn.model == "fake-model"
    parent = conversations.store[(THREAD, "inkling", "")]
    child = conversations.store[(THREAD, "claude", "repo-audit")]
    assert child.parent_conversation_id == parent.id
    # The hand was only addressed at the pre-ensured child conversation…
    assert turn.conversation_id == child.id
    # …and the spawner stamped the run tree after the report came back.
    _run_id, parent_run_id, parent_tool_call_id = conversations.parent_links[-1]
    assert (parent_run_id, parent_tool_call_id) == ("run-parent", "call-1")


async def test_an_accomplice_carries_no_gate_and_is_told_it_has_no_user() -> None:
    # Nested subagents do not exist: an accomplice runs with no capabilities —
    # no summon, no teleport, no schemes of its own — and its instructions tell
    # it it is an accomplice with no user.
    gate, agents, _, ctx = await _gate()
    claude = cast(FakeAgent, agents["claude"])
    await _tool(gate, SCHEME_TOOL_NAME)(
        ctx, name="hand", agent_id="claude", model="opus", brief="Work."
    )

    turn = claude.turns[0]
    assert turn.capabilities == []
    assert turn.instructions == ACCOMPLICE_INSTRUCTION
    assert turn.interactive is False


async def test_scheming_a_live_name_again_is_refused() -> None:
    gate, _, conversations, ctx = await _gate()
    scheme = _tool(gate, SCHEME_TOOL_NAME)
    await scheme(
        ctx, name="repo-audit", agent_id="claude", model="opus", brief="Go."
    )
    conversations.store[(THREAD, "claude", "repo-audit")].runs.append("run-child")

    with pytest.raises(ModelRetry, match="already at work"):
        await scheme(
            ctx, name="repo-audit", agent_id="claude", model="opus", brief="Again."
        )


async def test_whisper_continues_the_same_accomplice_in_a_later_parent_turn() -> None:
    gate, agents, conversations, ctx = await _gate()
    claude = cast(FakeAgent, agents["claude"])
    await _tool(gate, SCHEME_TOOL_NAME)(
        ctx, name="repo-audit", agent_id="claude", model="opus", brief="Audit."
    )

    parent = conversations.store[(THREAD, "inkling", "")]
    report = await _tool(gate, WHISPER_TOOL_NAME)(
        _ctx(parent.id, run_id="run-parent-2", tool_call_id="call-2"),
        name="repo-audit",
        message="Now fix finding two.",
    )

    assert report == "handled"
    follow_up = claude.turns[1]
    assert follow_up.run_name == "whisper"
    assert follow_up.prompt == "Now fix finding two."
    child = conversations.store[(THREAD, "claude", "repo-audit")]
    assert follow_up.conversation_id == child.id
    _run_id, parent_run_id, parent_tool_call_id = conversations.parent_links[-1]
    assert (parent_run_id, parent_tool_call_id) == ("run-parent-2", "call-2")
    # One conversation for the hand — the follow-up landed in the same context.
    assert len(
        [key for key in conversations.store if key[2] == "repo-audit"]
    ) == 1


async def test_whisper_with_an_unknown_name_lists_the_live_accomplices() -> None:
    gate, _, _, ctx = await _gate()
    await _tool(gate, SCHEME_TOOL_NAME)(
        ctx, name="repo-audit", agent_id="claude", model="opus", brief="Audit."
    )

    with pytest.raises(ModelRetry, match="repo-audit"):
        await _tool(gate, WHISPER_TOOL_NAME)(
            ctx, name="wrong-name", message="hello?"
        )


async def test_scheme_refuses_self_bad_routes_and_unclaimed_effort() -> None:
    gate, _, _, ctx = await _gate()
    scheme = _tool(gate, SCHEME_TOOL_NAME)

    with pytest.raises(ModelRetry, match="Cannot scheme with yourself"):
        await scheme(
            ctx, name="me", agent_id="inkling", model="opus", brief="Hi."
        )
    with pytest.raises(ModelRetry, match="Invalid scheme route"):
        await scheme(
            ctx, name="x", agent_id="claude", model="haiku", brief="Hi."
        )
    with pytest.raises(ModelRetry, match="does not accept effort"):
        await scheme(
            ctx,
            name="x",
            agent_id="claude",
            model="opus",
            brief="Hi.",
            effort="xhigh",
        )


async def test_a_deferring_accomplice_fails_loudly_instead_of_parking() -> None:
    gate, agents, _, ctx = await _gate()
    claude = cast(FakeAgent, agents["claude"])
    claude.reception_output = DeferredToolRequests(
        calls=[
            ToolCallPart(
                tool_name="ask_questions",
                args={"questions": [{"question": "?"}]},
                tool_call_id="call_ask",
            )
        ]
    )

    with pytest.raises(ModelRetry, match="has no user"):
        await _tool(gate, SCHEME_TOOL_NAME)(
            ctx, name="asker", agent_id="claude", model="opus", brief="Go."
        )


@dataclass
class SlowAgent(FakeAgent):
    delay: float = 0.1

    async def run(self, *args: object, **kwargs: object):  # pyright: ignore[reportIncompatibleMethodOverride]
        await asyncio.sleep(self.delay)
        return await super().run(*args, **kwargs)  # pyright: ignore[reportArgumentType]


async def test_an_overrunning_accomplice_fails_the_tool_not_the_turn() -> None:
    agents = {
        "inkling": cast(AgentTentacle, FakeAgent(id="inkling")),
        "claude": cast(
            AgentTentacle,
            SlowAgent(id="claude", allow_reception_run=True, delay=0.5),
        ),
    }
    gate, _, _, ctx = await _gate(agents=agents, scheme_timeout=0.05)

    with pytest.raises(ModelRetry, match="exceeded"):
        await _tool(gate, SCHEME_TOOL_NAME)(
            ctx, name="slow", agent_id="claude", model="opus", brief="Take ages."
        )


async def test_three_schemes_in_one_reply_run_concurrently() -> None:
    agents = {
        "inkling": cast(AgentTentacle, FakeAgent(id="inkling")),
        "claude": cast(
            AgentTentacle,
            SlowAgent(id="claude", allow_reception_run=True, delay=0.1),
        ),
    }
    gate, _, conversations, ctx = await _gate(agents=agents)
    parent_id = conversations.store[(THREAD, "inkling", "")].id
    scheme = _tool(gate, SCHEME_TOOL_NAME)

    started = time.monotonic()
    reports = await asyncio.gather(
        *(
            scheme(
                _ctx(parent_id, run_id="run-parent", tool_call_id=f"call-{i}"),
                name=f"hand-{i}",
                agent_id="claude",
                model="opus",
                brief=f"Task {i}.",
            )
            for i in range(3)
        )
    )
    elapsed = time.monotonic() - started

    assert reports == ["handled"] * 3
    # Three 0.1s hands in well under 0.3s: they ran concurrently, and all three
    # child conversations exist.
    assert elapsed < 0.28
    assert {key[2] for key in conversations.store if key[2]} == {
        "hand-0",
        "hand-1",
        "hand-2",
    }


async def test_a_gate_without_scheme_deps_offers_no_scheme() -> None:
    bare = GatewayCapability(routes=[CLAUDE_ROUTE], current_agent_id="inkling")
    assert bare.toolset is not None
    assert SCHEME_TOOL_NAME not in bare.toolset.tools
    assert not bare.scheming
    assert SCHEME_TOOL_NAME not in bare.get_instructions()

    gate, _, _, ctx = await _gate()
    assert SCHEME_TOOL_NAME in gate.get_instructions()
