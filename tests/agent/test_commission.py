"""The gate's commission spells: an ordinary awaited tool call that runs another
agent in its own subagent conversation and returns the report — plus the guards
that keep an accomplice an accomplice (no gate of its own, bounded time, loud
failure instead of a parked deferral)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

import pytest
from arcanus import Relation
from pydantic import UUID7
from pydantic_ai import AgentStreamEvent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.tools import DeferredToolRequests
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.events import (
    SubagentSettledEvent,
    SubagentStartedEvent,
)
from octomate.managers.gateway import (
    ACCOMPLICE_INSTRUCTION,
    GatewayRefusal,
    OctomateSession,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.thread import Thread, ThreadKey
from octomate.schemas.triage import (
    AgentRoute,
    Claim,
)
from octomate.tentacles.agent import AgentTentacle
from octomate.tentacles.inkling.mcp import Accomplices
from tests.support.agents import FakeAgent
from tests.support.managers import (
    FakeConversationManager,
    FakeThreadManager,
    a_project,
    a_registry,
    a_workspace_manager,
)
from tests.support.users import a_user

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


class ParentCall(TypedDict):
    parent_id: UUID7
    parent_run_id: str | None
    parent_tool_call_id: str | None
    conversations: FakeConversationManager
    commission_timeout: float


async def _gate(
    *,
    agents: dict[str, AgentTentacle] | None = None,
    conversations: FakeConversationManager | None = None,
    commission_timeout: float = 5.0,
) -> tuple[
    OctomateSession,
    dict[str, AgentTentacle],
    FakeConversationManager,
    ParentCall,
]:
    agents = agents or {
        "inkling": cast(AgentTentacle, FakeAgent(id="inkling")),
        "claude": cast(AgentTentacle, FakeAgent(id="claude")),
    }
    conversations = conversations or FakeConversationManager()
    threads = FakeThreadManager()
    thread = await threads.ensure(ADDRESS)
    threads.threads_by_key[thread.key] = Thread(
        id=THREAD,
        kind="dm",
        chat_type="dm",
        chat_id=ADDRESS.chat_id,
        channel_tentacle_id=ADDRESS.channel_tentacle_id,
        project=Relation(None),
    )
    for agent in agents.values():
        agent.conversations = conversations
    gate = OctomateSession(
        channel_routes={"im": [CLAUDE_ROUTE]},
        current_agent_id="inkling",
        agents=agents,
        thread_id=THREAD,
        conversation_address=ADDRESS,
        threads=threads,
        workspaces=a_workspace_manager(),
        users=threads.users,
    )
    parent = await conversations.ensure(THREAD, agent_tentacle_id="inkling")
    return (
        gate,
        agents,
        conversations,
        ParentCall(
            parent_id=parent.id,
            parent_run_id="run-parent",
            parent_tool_call_id="call-1",
            conversations=conversations,
            commission_timeout=commission_timeout,
        ),
    )


async def test_commission_runs_the_accomplice_and_returns_its_report() -> None:
    gate, agents, conversations, ctx = await _gate()
    claude = cast(FakeAgent, agents["claude"])
    claude.reception_output = "audit: three findings"

    report = await gate.commission(
        **ctx,
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
    await gate.commission(
        **ctx, name="hand", agent_id="claude", model="opus", brief="Work."
    )

    turn = claude.turns[0]
    assert turn.capabilities == []
    assert turn.instructions == ACCOMPLICE_INSTRUCTION
    assert turn.interactive is False


async def test_commissioning_a_live_name_again_is_refused() -> None:
    gate, _, conversations, ctx = await _gate()
    commission = gate.commission
    await commission(
        **ctx, name="repo-audit", agent_id="claude", model="opus", brief="Go."
    )
    child = await conversations.ensure(
        THREAD, agent_tentacle_id="claude", subagent_id="repo-audit"
    )
    await conversations.record_agent_run(child, str(uuid7()), [])

    with pytest.raises(GatewayRefusal, match="already at work"):
        await commission(
            **ctx, name="repo-audit", agent_id="claude", model="opus", brief="Again."
        )


async def test_whisper_continues_the_same_accomplice_in_a_later_parent_turn() -> None:
    gate, agents, conversations, ctx = await _gate()
    claude = cast(FakeAgent, agents["claude"])
    await gate.commission(
        **ctx, name="repo-audit", agent_id="claude", model="opus", brief="Audit."
    )

    report = await gate.whisper(
        **{**ctx, "parent_run_id": "run-parent-2", "parent_tool_call_id": "call-2"},
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
    await gate.commission(
        **ctx, name="repo-audit", agent_id="claude", model="opus", brief="Audit."
    )

    with pytest.raises(GatewayRefusal, match="repo-audit"):
        await gate.whisper(**ctx, name="wrong-name", message="hello?")


async def test_commission_refuses_self_bad_routes_and_unclaimed_effort() -> None:
    gate, _, _, ctx = await _gate()
    commission = gate.commission

    with pytest.raises(GatewayRefusal, match="Cannot commission yourself"):
        await commission(
            **ctx, name="me", agent_id="inkling", model="opus", brief="Hi."
        )
    with pytest.raises(GatewayRefusal, match="Invalid commission route"):
        await commission(**ctx, name="x", agent_id="claude", model="haiku", brief="Hi.")
    with pytest.raises(GatewayRefusal, match="does not accept effort"):
        await commission(
            **ctx,
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

    with pytest.raises(GatewayRefusal, match="has no user"):
        await gate.commission(
            **ctx, name="asker", agent_id="claude", model="opus", brief="Go."
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
            SlowAgent(id="claude", delay=0.5),
        ),
    }
    gate, _, _, ctx = await _gate(agents=agents, commission_timeout=0.05)

    with pytest.raises(GatewayRefusal, match="exceeded"):
        await gate.commission(
            **ctx, name="slow", agent_id="claude", model="opus", brief="Take ages."
        )


async def test_three_commissions_in_one_reply_run_concurrently() -> None:
    agents = {
        "inkling": cast(AgentTentacle, FakeAgent(id="inkling")),
        "claude": cast(
            AgentTentacle,
            SlowAgent(id="claude", delay=0.1),
        ),
    }
    gate, _, conversations, ctx = await _gate(agents=agents)
    commission = gate.commission

    started = time.monotonic()
    reports = await asyncio.gather(
        *(
            commission(
                **{**ctx, "parent_tool_call_id": f"call-{i}"},
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


async def test_accomplice_requires_a_live_core_session() -> None:
    gate, _, _, ctx = await _gate()
    gate.agents = None
    with pytest.raises(RuntimeError, match="need agents"):
        await gate.commission(
            **ctx, name="probe", agent_id="claude", model="opus", brief="Go."
        )


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
        ToolReturnPart(
            tool_name="commission",
            content="The accomplice exceeded its timeout.",
            tool_call_id="call-timeout",
            outcome="retried",
        )
    )
    nameless = _call("call-nameless", brief="no name")

    watched = await _watched([timeout, retry, nameless])

    [started, _, settled, _, _] = watched
    assert isinstance(started, SubagentStartedEvent)
    assert isinstance(settled, SubagentSettledEvent)
    assert settled.status == "failed"
    assert "The accomplice exceeded its timeout." in settled.response


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


async def test_commission_inherits_the_parent_owner_in_a_project(
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    project = a_project(root)
    app = Octomate()
    app.workspaces.projects = await a_registry(project)
    owner = await a_user("alice", profiles={"im": "alice"})
    thread = await app.threads.ensure(
        ThreadKey("im", "thread", "shared", "turn"), project=project
    )
    gate, agents, conversations, ctx = await _gate(
        agents={
            "inkling": cast(AgentTentacle, FakeAgent(id="inkling")),
            "claude": cast(
                AgentTentacle, FakeAgent(id="claude", workspaces=app.workspaces)
            ),
        }
    )
    gate.thread_id = thread.id
    gate.threads = app.threads
    gate.workspaces = app.workspaces
    gate.user_profile = await app.users.profile("im", "alice")
    parent = await conversations.ensure(thread.id, agent_tentacle_id="inkling")
    ctx["parent_id"] = parent.id
    result = await gate.commission(
        **ctx, name="audit", agent_id="claude", model="opus", brief="Audit."
    )
    assert result == "handled"
    child = agents["claude"]
    assert isinstance(child, FakeAgent)
    [turn] = child.turns
    assert turn.session is None
    assert turn.invocation.owner_id == owner.id
    assert turn.invocation.thread_id == thread.id
    assert turn.invocation.storage.path.is_dir()
