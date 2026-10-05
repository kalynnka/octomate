"""The gate's commission spells: an ordinary awaited tool call that runs another
agent in its own subagent conversation and returns the report — plus the guards
that keep an accomplice an accomplice (no gate of its own, bounded time, loud
failure instead of a parked deferral)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

import pytest
from octomate_protocol.gateway import GatewayTool
from pydantic import UUID7
from pydantic_ai import AgentStreamEvent, RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.tools import DeferredToolRequests
from uuid_utils.compat import uuid7

from octomate.capabilities.gateway import (
    ACCOMPLICE_INSTRUCTION,
    Accomplices,
    GatewayCapability,
)
from octomate.capabilities.harness.events import (
    SubagentSettledEvent,
    SubagentStartedEvent,
)
from octomate.managers.gateway import OctomateSession
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import (
    AgentRoute,
    Claim,
)
from octomate.tentacles.agent import AgentTentacle
from tests.support.agents import FakeAgent
from tests.support.managers import FakeConversationManager

THREAD = uuid7()
ADDRESS = ChannelAddress(
    channel_tentacle_id="im",
    chat_type="dm",
    chat_id="alice",
    user_id="alice",
    channel_thread_id=None,
)
CLAUDE_ROUTE = AgentRoute(
    agent_id="claude",
    model="opus",
    claim=Claim(ability="coding work", efforts=("low", "medium", "high")),
)


def _ctx(
    parent_id: UUID7,
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
    commission_timeout: float = 5.0,
) -> tuple[
    GatewayCapability,
    dict[str, AgentTentacle],
    FakeConversationManager,
    RunContext[None],
]:
    agents = agents or {
        "inkling": cast(AgentTentacle, FakeAgent(id="inkling")),
        "claude": cast(AgentTentacle, FakeAgent(id="claude", allow_reception_run=True)),
    }
    conversations = conversations or FakeConversationManager()
    gate = GatewayCapability(
        session=OctomateSession(
            channel_routes={"im": [CLAUDE_ROUTE]},
            current_agent_id="inkling",
            agents=agents,
            thread_id=THREAD,
            conversation_address=ADDRESS,
        ),
        conversations=conversations,
        commission_timeout=commission_timeout,
    )
    parent = await conversations.ensure(THREAD, agent_tentacle_id="inkling")
    return gate, agents, conversations, _ctx(parent.id)


def _tool(gate: GatewayCapability, name: str):
    assert gate.toolset is not None
    return gate.toolset.tools[name].function


async def test_commission_runs_the_accomplice_and_returns_its_report() -> None:
    gate, agents, conversations, ctx = await _gate()
    claude = cast(FakeAgent, agents["claude"])
    claude.reception_output = "audit: three findings"

    report = await _tool(gate, GatewayTool.COMMISSION)(
        ctx,
        name="repo-audit",
        agent_id="claude",
        model="opus",
        brief="Audit the repo.",
    )

    assert report == "audit: three findings"
    turn = claude.turns[0]
    assert turn.prompt == "Audit the repo."
    assert turn.run_name == "commission"
    assert turn.thread_id == THREAD
    assert turn.address == ADDRESS
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
    # no summon, no teleport, no commissions of its own — and its instructions tell
    # it it is an accomplice with no user.
    gate, agents, _, ctx = await _gate()
    claude = cast(FakeAgent, agents["claude"])
    await _tool(gate, GatewayTool.COMMISSION)(
        ctx, name="hand", agent_id="claude", model="opus", brief="Work."
    )

    turn = claude.turns[0]
    assert turn.capabilities == []
    assert turn.instructions == ACCOMPLICE_INSTRUCTION
    assert turn.interactive is False


async def test_commissioning_a_live_name_again_is_refused() -> None:
    gate, _, conversations, ctx = await _gate()
    commission = _tool(gate, GatewayTool.COMMISSION)
    await commission(
        ctx, name="repo-audit", agent_id="claude", model="opus", brief="Go."
    )
    child = await conversations.ensure(
        THREAD, agent_tentacle_id="claude", subagent_id="repo-audit"
    )
    await conversations.record_agent_run(child, str(uuid7()), [])

    with pytest.raises(ModelRetry, match="already at work"):
        await commission(
            ctx, name="repo-audit", agent_id="claude", model="opus", brief="Again."
        )


async def test_whisper_continues_the_same_accomplice_in_a_later_parent_turn() -> None:
    gate, agents, conversations, ctx = await _gate()
    claude = cast(FakeAgent, agents["claude"])
    await _tool(gate, GatewayTool.COMMISSION)(
        ctx, name="repo-audit", agent_id="claude", model="opus", brief="Audit."
    )

    parent = conversations.store[(THREAD, "inkling", "")]
    report = await _tool(gate, GatewayTool.WHISPER)(
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
    assert len([key for key in conversations.store if key[2] == "repo-audit"]) == 1


async def test_whisper_with_an_unknown_name_lists_the_live_accomplices() -> None:
    gate, _, _, ctx = await _gate()
    await _tool(gate, GatewayTool.COMMISSION)(
        ctx, name="repo-audit", agent_id="claude", model="opus", brief="Audit."
    )

    with pytest.raises(ModelRetry, match="repo-audit"):
        await _tool(gate, GatewayTool.WHISPER)(ctx, name="wrong-name", message="hello?")


async def test_commission_refuses_self_bad_routes_and_unclaimed_effort() -> None:
    gate, _, _, ctx = await _gate()
    commission = _tool(gate, GatewayTool.COMMISSION)

    with pytest.raises(ModelRetry, match="Cannot commission yourself"):
        await commission(ctx, name="me", agent_id="inkling", model="opus", brief="Hi.")
    with pytest.raises(ModelRetry, match="Invalid commission route"):
        await commission(ctx, name="x", agent_id="claude", model="haiku", brief="Hi.")
    with pytest.raises(ModelRetry, match="does not accept effort"):
        await commission(
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
        await _tool(gate, GatewayTool.COMMISSION)(
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
    gate, _, _, ctx = await _gate(agents=agents, commission_timeout=0.05)

    with pytest.raises(ModelRetry, match="exceeded"):
        await _tool(gate, GatewayTool.COMMISSION)(
            ctx, name="slow", agent_id="claude", model="opus", brief="Take ages."
        )


async def test_three_commissions_in_one_reply_run_concurrently() -> None:
    agents = {
        "inkling": cast(AgentTentacle, FakeAgent(id="inkling")),
        "claude": cast(
            AgentTentacle,
            SlowAgent(id="claude", allow_reception_run=True, delay=0.1),
        ),
    }
    gate, _, conversations, _ = await _gate(agents=agents)
    parent_id = conversations.store[(THREAD, "inkling", "")].id
    commission = _tool(gate, GatewayTool.COMMISSION)

    started = time.monotonic()
    reports = await asyncio.gather(
        *(
            commission(
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


async def test_a_gate_without_commission_deps_offers_no_commission() -> None:
    bare = GatewayCapability(
        session=OctomateSession(
            channel_routes={"im": [CLAUDE_ROUTE]}, current_agent_id="inkling"
        )
    )
    assert bare.toolset is not None
    assert GatewayTool.COMMISSION not in bare.toolset.tools
    assert not bare.commissioning
    assert GatewayTool.COMMISSION not in bare.get_instructions()

    gate, _, _, _ = await _gate()
    assert GatewayTool.COMMISSION in gate.get_instructions()


async def _watched(
    events: list[FunctionToolCallEvent | FunctionToolResultEvent],
) -> list[AgentStreamEvent]:
    async def stream() -> AsyncIterator[AgentStreamEvent]:
        for event in events:
            yield event

    return [event async for event in Accomplices().watch(stream())]


def _call(
    tool_call_id: str, tool: str = "commission", **args: str
) -> FunctionToolCallEvent:
    return FunctionToolCallEvent(
        ToolCallPart(tool_name=tool, args=args, tool_call_id=tool_call_id)
    )


async def test_an_accomplice_starts_before_its_call_and_settles_before_its_result() -> (
    None
):
    first = _call("call-a", name="audit")
    second = _call("call-b", tool="whisper", name="tests")
    done = FunctionToolResultEvent(
        ToolReturnPart(
            tool_name="whisper", content="test report", tool_call_id="call-b"
        )
    )

    watched = await _watched([first, second, done])

    assert watched == [
        SubagentStartedEvent(invocation_id="call-a", kind="commission", name="audit"),
        first,
        SubagentStartedEvent(invocation_id="call-b", kind="whisper", name="tests"),
        second,
        SubagentSettledEvent(
            invocation_id="call-b", status="completed", response="test report"
        ),
        done,
        # Never answered in this stretch of the run, so it finishes with it.
        SubagentSettledEvent(invocation_id="call-a", status="failed"),
    ]


async def test_a_refused_accomplice_settles_failed_and_a_nameless_one_never_starts() -> (
    None
):
    timeout = _call("call-timeout", name="timeout")
    retry = FunctionToolResultEvent(
        RetryPromptPart(
            tool_name="commission",
            content="The accomplice exceeded its timeout.",
            tool_call_id="call-timeout",
        )
    )
    nameless = _call("call-nameless", brief="no name")

    watched = await _watched([timeout, retry, nameless])

    [started, _, settled, _, _] = watched
    assert isinstance(started, SubagentStartedEvent)
    assert isinstance(settled, SubagentSettledEvent)
    assert settled.status == "failed"
    assert settled.response.startswith("The accomplice exceeded its timeout.")


async def test_an_accomplice_left_running_settles_as_cancelled_with_the_run() -> None:
    waiting = asyncio.Event()
    received: list[AgentStreamEvent] = []

    async def stream() -> AsyncIterator[AgentStreamEvent]:
        yield _call("call-a", name="cancelled")
        waiting.set()
        await asyncio.Event().wait()

    async def consume() -> None:
        async for event in Accomplices().watch(stream()):
            received.append(event)

    task = asyncio.create_task(consume())
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert received[-1] == SubagentSettledEvent(
        invocation_id="call-a", status="cancelled"
    )
