"""The collapsed dispatch graph at the node level: Awake, Route, React,
Handoff, and ResumeDeferred — driven with the canonical fake
agent/channel/managers. End-to-end behavior lives in test_dispatch.py."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import ClassVar, cast

import pytest
from pydantic_ai import AgentCapability, AgentRunResult, AgentRunResultEvent, RunContext
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.settings import ThinkingEffort
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from octomate.capabilities.gateway import GatewayCapability
from octomate.capabilities.harness.events import MessageSentEvent
from octomate.config import AgentModelConfig, ChannelConfig, ChannelStreamConfig
from octomate.config.users import UserConfig
from octomate.managers.deferred import DeferredActionManager
from octomate.managers.gateway import GatewayManager
from octomate.managers.user import UserManager
from octomate.managers.workspaces import WorkspaceManager
from octomate.reflex import (
    DeferredResult,
    ReflexDeps,
    ReflexState,
    ResponseTarget,
    SummonDecision,
)
from octomate.reflex.graph import (
    Awake,
    React,
    ReflexGraphResult,
    ResumeDeferred,
    Route,
    build_reflex_graph,
)
from octomate.schemas.awakes import (
    DeferredActionBatchResponse,
    GatewayHandoffSignal,
    UserMessageSignal,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import DeferredQuestion
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import MarkdownSegment, TextSegment
from octomate.schemas.thread import Thread, ThreadKey
from octomate.schemas.triage import (
    SCRY_TOOL_NAME,
    AgentRoute,
    Claim,
    CrossingLanding,
    HereLanding,
    SchemeDecision,
    SummonLanding,
    ThreadLanding,
)
from octomate.schemas.user import UserProfile
from octomate.tentacles.channels.base import ChannelSurfaces
from octomate.tentacles.channels.feelers.output import TimelineState
from octomate.types.threads import CLAUDE_NATIVE_ID
from tests.support.agents import FakeAgent, RecordedRun
from tests.support.channels import FakeChannelTentacle, RecordingInk
from tests.support.managers import (
    FakeActionManager,
    FakeConversationManager,
    FakeDeferredBatch,
    FakePresentedBatch,
    FakeThreadManager,
    FakeUserManager,
    RecordingWorkspaceManager,
)

FAKE_CONTEXT = cast(RunContext[None], None)


async def _run(
    entry: Awake | Route | React | ResumeDeferred,
    *,
    state: ReflexState,
    deps: ReflexDeps,
) -> ReflexGraphResult:
    """Run the reflex nodes from `entry`. Production always enters at `Awake`; a
    test wires the same nodes with a different door to exercise a stretch of them
    on its own."""
    return await build_reflex_graph(type(entry)).run(
        inputs=entry, state=state, deps=deps
    )


def _channel(*, stream: bool = True) -> FakeChannelTentacle:
    return FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=stream),
            agents=[AgentModelConfig(agent="other", model="test")],
        )
    )


class DroppingTimelineState(TimelineState):
    async def drive(self, stream: AsyncIterator[object]) -> None:
        return  # abandon the reception stream without draining it


class DroppingTimelineFeeler:
    @asynccontextmanager
    async def open(
        self, address: ChannelAddress
    ) -> AsyncGenerator[DroppingTimelineState]:
        yield DroppingTimelineState()


class DroppingChannel(FakeChannelTentacle):
    """A channel whose timeline abandons the reception stream without producing a
    result (exercises the fail-fast guard)."""

    def __init__(self, *, config: ChannelConfig) -> None:
        super().__init__(config=config)
        self.feelers.timeline = DroppingTimelineFeeler()


def _key(thread_id: str = "") -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="im",
        chat_type="dm",
        chat_id="alice",
        user_id="alice",
        channel_thread_id=thread_id,
    )


def _source_target(address: ChannelAddress) -> ResponseTarget:
    return ResponseTarget(
        channel_id="im",
        address=address,
        thread_strategy="flat_thread",
        mode="main",
    )


def _thread(address: ChannelAddress) -> Thread:
    key = ThreadKey.from_address(address)
    return Thread(
        channel_tentacle_id=key.channel_tentacle_id,
        chat_type=key.chat_type,
        chat_id=key.chat_id,
        channel_thread_id=key.channel_thread_id,
        kind=key.kind,
    )


def _state(
    address: ChannelAddress,
    *,
    user_prompt: str | None = "hi",
    thread: Thread | None = None,
) -> ReflexState:
    return ReflexState(
        source_target=_source_target(address),
        user_prompt=user_prompt,
        thread=thread,
    )


def _deps(
    *,
    conversations: FakeConversationManager,
    channels: dict[str, FakeChannelTentacle],
    agent: FakeAgent,
    action_manager: FakeActionManager | None = None,
    workspaces: WorkspaceManager | None = None,
    gateway: GatewayManager | None = None,
) -> ReflexDeps:
    return ReflexDeps(
        workspaces=workspaces
        if workspaces is not None
        else RecordingWorkspaceManager(),
        channels=dict(channels),
        agents={"inkling": agent, "other": agent, agent.id: agent},
        conversation_manager=conversations,
        thread_manager=FakeThreadManager(),
        action_manager=cast(
            DeferredActionManager, action_manager or FakeActionManager()
        ),
        gateway=gateway or GatewayManager(),
    )


def _requests() -> DeferredToolRequests:
    return DeferredToolRequests(
        calls=[
            ToolCallPart(
                tool_name="ask_questions",
                args={"questions": [{"question": "What should I clarify?"}]},
                tool_call_id="call_question",
            )
        ]
    )


def _deferred_results() -> DeferredToolResults:
    results = DeferredToolResults()
    results.calls["call_question"] = ["please answer directly"]
    return results


def _summon(
    agent_id: str = "other",
    destination: SummonLanding | None = None,
    effort: ThinkingEffort | None = None,
) -> SummonDecision:
    return SummonDecision(
        action="summon",
        agent_id=agent_id,
        model="test",
        destination=destination or ThreadLanding(),
        effort=effort,
        reason="needs work",
        hint="Working on it",
        summon="Please debug this in reception.",
    )


def _summon_deps(
    im: FakeChannelTentacle,
    entry: FakeAgent,
    second: FakeAgent,
    far: FakeChannelTentacle | None = None,
) -> ReflexDeps:
    return ReflexDeps(
        workspaces=RecordingWorkspaceManager(),
        gateway=GatewayManager(),
        channels={"im": im} if far is None else {"im": im, "far": far},
        agents={entry.id: entry, second.id: second},
        conversation_manager=FakeConversationManager(),
        thread_manager=FakeThreadManager(),
        action_manager=cast(DeferredActionManager, FakeActionManager()),
    )


def _two_reception_config(*, stream: bool) -> ChannelConfig:
    return ChannelConfig(
        type="fake",
        stream=ChannelStreamConfig(enabled=stream),
        agents=[
            AgentModelConfig(agent="other", model="test"),
            AgentModelConfig(agent="second", model="test"),
        ],
    )


def _recorded_gate_capability(run: RecordedRun) -> GatewayCapability:
    gates = [
        capability
        for capability in run.capabilities
        if isinstance(capability, GatewayCapability)
    ]
    assert len(gates) == 1
    return gates[0]


def test_available_routes_skip_disconnected_reception_agents() -> None:
    channel = FakeChannelTentacle(
        id="chan1",
        config=ChannelConfig(
            type="fake",
            agents=[
                AgentModelConfig(agent="claude", model="opus"),
                AgentModelConfig(agent="other", model="test"),
            ],
        ),
    )
    other = FakeAgent(id="other", claims={"test": Claim(ability="fake agent")})
    deps = ReflexDeps(
        workspaces=RecordingWorkspaceManager(),
        gateway=GatewayManager(),
        channels={"chan1": channel},
        agents={"other": other},
        conversation_manager=FakeConversationManager(),
        thread_manager=FakeThreadManager(),
        action_manager=cast(DeferredActionManager, FakeActionManager()),
    )

    routes = deps.available_routes["chan1"]

    assert [(route.agent_id, route.model) for route in routes] == [("other", "test")]
    assert deps.available_routes["chan1"] is routes


def test_agent_routes_evict_models_the_agent_does_not_serve() -> None:
    served = Claim(ability="fake agent")
    agent = FakeAgent(claims={"test": served, "haiku": Claim(ability="phantom")})

    assert agent.routes == [AgentRoute(agent_id="inkling", model="test", claim=served)]


def test_available_routes_are_the_exposed_agents_own_routes() -> None:
    # A channel exposes agents; each agent's active claims are its routes. A
    # claimed model rides in even when no channel entry names it, an agent with
    # no claims contributes nothing, and duplicate channel entries for one
    # agent do not duplicate its routes.
    pro_claim = Claim(ability="deep work", efforts=("high",))
    channel = FakeChannelTentacle(
        id="chan1",
        config=ChannelConfig(
            type="fake",
            agents=[
                AgentModelConfig(agent="other", model="test"),
                AgentModelConfig(agent="second", model="test"),
                AgentModelConfig(agent="second", model="deepseek:deepseek-v4-pro"),
                AgentModelConfig(agent="third", model="test"),
            ],
        ),
    )
    other = FakeAgent(id="other", claims={"test": Claim(ability="fake agent")})
    second = FakeAgent(
        id="second",
        claims={
            "test": Claim(ability="fake agent"),
            "deepseek:deepseek-v4-pro": pro_claim,
        },
    )
    third = FakeAgent(id="third", claims={})
    deps = ReflexDeps(
        workspaces=RecordingWorkspaceManager(),
        gateway=GatewayManager(),
        channels={"chan1": channel},
        agents={"other": other, "second": second, "third": third},
        conversation_manager=FakeConversationManager(),
        thread_manager=FakeThreadManager(),
        action_manager=cast(DeferredActionManager, FakeActionManager()),
    )

    routes = deps.available_routes["chan1"]

    assert [(route.agent_id, route.model) for route in routes] == [
        ("other", "test"),
        ("second", "test"),
        ("second", "deepseek:deepseek-v4-pro"),
    ]
    assert routes[2].claim == pro_claim


def test_resolve_agent_honors_a_served_model_off_the_channel_list() -> None:
    # A summoned model is claims-driven; resolve must not snap it back to the
    # channel's entry model when the agent serves the requested one.
    channel = FakeChannelTentacle(
        id="chan1",
        config=ChannelConfig(
            type="fake",
            agents=[AgentModelConfig(agent="other", model="test")],
        ),
    )
    other = FakeAgent(id="other")
    deps = ReflexDeps(
        workspaces=RecordingWorkspaceManager(),
        gateway=GatewayManager(),
        channels={"chan1": channel},
        agents={"other": other},
        conversation_manager=FakeConversationManager(),
        thread_manager=FakeThreadManager(),
        action_manager=cast(DeferredActionManager, FakeActionManager()),
    )

    resolved = deps.resolve_agent("chan1", "other", "deepseek:deepseek-v4-pro")

    assert (resolved.agent, resolved.model) == ("other", "deepseek:deepseek-v4-pro")


async def test_route_runs_entry_agent_directly() -> None:
    # No triage screen: Route dispatches a fresh message straight to the channel's
    # default agent, which answers in one reception run.
    address = _key()
    agent = FakeAgent(id="other", reception_output="hello")
    conversations = FakeConversationManager()
    im = _channel()

    result = await _run(
        Route(),
        state=_state(address, user_prompt="hi"),
        deps=_deps(conversations=conversations, channels={"im": im}, agent=agent),
    )

    assert not isinstance(result, DeferredResult)
    assert result.result is not None
    assert result.result.output == "hello"
    assert [stream.run_name for stream in agent.streams] == ["react"]
    assert agent.turns == []
    assert im.consumed[0][0] == address
    assert im.sent[-1][2][0]["text"] == "hello"


async def test_route_entry_run_claims_no_ownership() -> None:
    address = _key()
    agent = FakeAgent(id="other", reception_output="hello")
    thread = _thread(address)
    conversations = FakeConversationManager()
    im = _channel()

    await _run(
        Route(),
        state=_state(address, user_prompt="hi", thread=thread),
        deps=_deps(conversations=conversations, channels={"im": im}, agent=agent),
    )

    assert list(thread.handoffs) == []


async def test_reception_mounts_gate_capability() -> None:
    address = _key()
    agent = FakeAgent(id="other", allow_reception_run=True, reception_output="done")
    conversations = FakeConversationManager()
    im = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=False),
            agents=[AgentModelConfig(agent="other", model="test")],
        )
    )
    target = _source_target(address)

    await _run(
        React(),
        state=ReflexState(source_target=target, target=target, decision=_summon()),
        deps=_deps(conversations=conversations, channels={"im": im}, agent=agent),
    )

    gate = _recorded_gate_capability(agent.turns[0])
    assert gate.toolset is not None
    scry = gate.toolset.tools[SCRY_TOOL_NAME].function
    routes = await scry(FAKE_CONTEXT, "routes")
    places = await scry(FAKE_CONTEXT, "destinations")
    assert routes == []
    # One list for every spell: this run is a DM, so `dm` is not among them — it is
    # already where it would go — and nothing links this asker to another channel.
    assert [one.handle for one in places] == ["here"]


async def test_non_stream_reception_presents_only_the_final_output() -> None:
    address = _key()
    agent = FakeAgent(id="other", allow_reception_run=True, reception_output="done")
    conversations = FakeConversationManager()
    im = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=False),
            agents=[AgentModelConfig(agent="other", model="test")],
        )
    )
    target = _source_target(address)

    await _run(
        React(),
        state=ReflexState(source_target=target, target=target, decision=_summon()),
        deps=_deps(conversations=conversations, channels={"im": im}, agent=agent),
    )

    assert len(agent.turns) == 1
    assert agent.streams == []
    assert im.consumed == []
    assert im.sent[-1][2][0]["text"] == "done"


async def test_react_mounts_a_commissioning_gate_in_a_thread() -> None:
    address = _key("t1")
    agent = FakeAgent(id="other", allow_reception_run=True, reception_output="done")
    conversations = FakeConversationManager()
    im = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=False),
            agents=[AgentModelConfig(agent="other", model="test")],
        )
    )
    target = _source_target(address)
    thread = _thread(address)

    await _run(
        React(),
        state=ReflexState(
            source_target=target, target=target, decision=_summon(), thread=thread
        ),
        deps=_deps(conversations=conversations, channels={"im": im}, agent=agent),
    )

    gate = _recorded_gate_capability(agent.turns[0])
    assert gate.commissioning
    assert gate.session.thread_id == thread.id
    assert gate.session.conversation_address == address
    assert gate.toolset is not None
    assert "commission" in gate.toolset.tools


async def test_a_finished_turn_saves_the_threads_workspace() -> None:
    # OCTO-51: a turn is over when its answer is delivered, and that is when its
    # work has to be somewhere the workspace going away cannot take it.
    address = _key("t1")
    agent = FakeAgent(id="other", allow_reception_run=True, reception_output="done")
    host = RecordingWorkspaceManager()
    im = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=False),
            agents=[AgentModelConfig(agent="other", model="test")],
        )
    )
    target = _source_target(address)
    thread = _thread(address)

    await _run(
        React(),
        state=ReflexState(
            source_target=target, target=target, decision=_summon(), thread=thread
        ),
        deps=_deps(
            conversations=FakeConversationManager(),
            channels={"im": im},
            agent=agent,
            workspaces=host,
        ),
    )

    assert host.saved == [thread.id]


async def test_a_turn_that_failed_still_saves_its_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A turn that raised still did whatever it did on disk, and until the save runs
    # the workspace is the only copy of it. It is also the only workspace the sweep
    # can never reclaim, since it keeps anything the mirror has not seen — so a
    # failed turn on a thread nobody resumes would hold its disk for good.
    address = _key("t1")
    agent = FakeAgent(id="other", allow_reception_run=True, reception_output="done")
    host = RecordingWorkspaceManager()
    im = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=False),
            agents=[AgentModelConfig(agent="other", model="test")],
        )
    )
    target = _source_target(address)
    thread = _thread(address)

    async def gave_out(*_args: object, **_kwargs: object) -> AgentRunResult[str]:
        raise RuntimeError("the provider gave out")

    monkeypatch.setattr(agent, "run", gave_out)

    with pytest.raises(RuntimeError, match="the provider gave out"):
        await _run(
            React(),
            state=ReflexState(
                source_target=target, target=target, decision=_summon(), thread=thread
            ),
            deps=_deps(
                conversations=FakeConversationManager(),
                channels={"im": im},
                agent=agent,
                workspaces=host,
            ),
        )

    assert host.saved == [thread.id]


async def test_a_turn_with_no_thread_saves_nothing() -> None:
    # A workspace belongs to a thread, so a turn without one has none to save.
    address = _key()
    agent = FakeAgent(id="other", allow_reception_run=True, reception_output="done")
    host = RecordingWorkspaceManager()
    im = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=False),
            agents=[AgentModelConfig(agent="other", model="test")],
        )
    )
    target = _source_target(address)

    await _run(
        React(),
        state=ReflexState(source_target=target, target=target, decision=_summon()),
        deps=_deps(
            conversations=FakeConversationManager(),
            channels={"im": im},
            agent=agent,
            workspaces=host,
        ),
    )

    assert host.saved == []


async def test_react_keys_the_gateway_session_by_the_turns_conversation() -> None:
    """React ensures the same (thread, agent) conversation the run resolves, so an
    external runtime's tool call finds this turn's session by the conversation id
    it already knows — and nothing stays registered once the turn ends."""
    address = _key()
    agent = FakeAgent(id="other", allow_reception_run=True, reception_output="done")
    conversations = FakeConversationManager()
    im = FakeChannelTentacle(config=_two_reception_config(stream=False))
    registry = GatewayManager()
    thread = _thread(address)
    target = _source_target(address)

    await _run(
        React(),
        state=ReflexState(
            source_target=target, target=target, decision=_summon(), thread=thread
        ),
        deps=_deps(
            conversations=conversations,
            channels={"im": im},
            agent=agent,
            gateway=registry,
        ),
    )

    gate = _recorded_gate_capability(agent.turns[0])
    conversation = await conversations.ensure(thread.id, agent_tentacle_id="other")
    assert gate.session.conversation_id == conversation.id
    assert registry.sessions == {}


@dataclass
class SlowAgent(FakeAgent):
    """A fake that takes its time, so two turns of one conversation can overlap."""

    delay: float = 0.1

    # Only delays; every argument passes through to the fake untouched.
    async def run(self, *args: object, **kwargs: object):  # pyright: ignore[reportIncompatibleMethodOverride]
        await asyncio.sleep(self.delay)
        return await super().run(*args, **kwargs)  # pyright: ignore[reportArgumentType]


async def test_a_second_turn_racing_a_live_conversation_fails_before_it_starts() -> (
    None
):
    """Nothing serialises two turns of one conversation, so the registry refuses
    the second at the door: the first arrival keeps its session, the second run
    raises before anything is presented, and the slot frees when the first ends."""
    address = _key()
    agent = SlowAgent(id="other", allow_reception_run=True, reception_output="done")
    conversations = FakeConversationManager()
    im = FakeChannelTentacle(config=_two_reception_config(stream=False))
    registry = GatewayManager()
    thread = _thread(address)
    target = _source_target(address)

    def turn() -> ReflexState:
        return ReflexState(
            source_target=target, target=target, decision=_summon(), thread=thread
        )

    deps = _deps(
        conversations=conversations,
        channels={"im": im},
        agent=agent,
        gateway=registry,
    )
    first, second = await asyncio.gather(
        _run(React(), state=turn(), deps=deps),
        _run(React(), state=turn(), deps=deps),
        return_exceptions=True,
    )

    assert not isinstance(first, BaseException)
    assert isinstance(second, RuntimeError)
    assert "already has a turn at the gateway" in str(second)
    # Only the first arrival ever ran, and nothing outlives its turn.
    assert len(agent.turns) == 1
    assert registry.sessions == {}


async def test_an_agent_with_gateway_off_mounts_none_on_any_channel() -> None:
    """Availability is the agent's own: with its flag off, no turn of it on any
    channel mounts a gateway capability."""
    address = _key()
    agent = FakeAgent(
        id="other", allow_reception_run=True, reception_output="done", gateway=False
    )
    conversations = FakeConversationManager()
    im = FakeChannelTentacle(config=_two_reception_config(stream=False))
    target = _source_target(address)

    await _run(
        React(),
        state=ReflexState(
            source_target=target,
            target=target,
            decision=_summon(),
            thread=_thread(address),
        ),
        deps=_deps(conversations=conversations, channels={"im": im}, agent=agent),
    )

    assert not [
        capability
        for capability in agent.turns[0].capabilities
        if isinstance(capability, GatewayCapability)
    ]


async def test_react_passes_the_decision_effort_to_the_run() -> None:
    address = _key()
    agent = FakeAgent(id="other", allow_reception_run=True, reception_output="done")
    conversations = FakeConversationManager()
    im = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=False),
            agents=[AgentModelConfig(agent="other", model="test")],
        )
    )
    target = _source_target(address)

    await _run(
        React(),
        state=ReflexState(
            source_target=target, target=target, decision=_summon(effort="high")
        ),
        deps=_deps(conversations=conversations, channels={"im": im}, agent=agent),
    )

    assert agent.turns[0].effort == "high"


async def test_reception_allow_here_false_on_group_main() -> None:
    # A group main channel refuses `summon here` (Case 1): the mounted gate is built
    # with allow_here=False so the model is steered to a thread.
    address = ChannelAddress(
        channel_tentacle_id="im",
        chat_type="group",
        chat_id="team",
        user_id="alice",
        channel_thread_id=None,
    )
    agent = FakeAgent(id="other", allow_reception_run=True, reception_output="done")
    conversations = FakeConversationManager()
    im = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=False),
            agents=[AgentModelConfig(agent="other", model="test")],
        )
    )
    target = ResponseTarget(
        channel_id="im", address=address, thread_strategy="flat_thread", mode="main"
    )

    await _run(
        React(),
        state=ReflexState(source_target=target, target=target, decision=_summon()),
        deps=_deps(conversations=conversations, channels={"im": im}, agent=agent),
    )

    assert _recorded_gate_capability(agent.turns[0]).session.allow_here is False


async def test_reception_summons_another_agent_into_sub_thread() -> None:
    address = _key()
    entry = FakeAgent(
        id="other",
        reception_summon=_summon(agent_id="second"),
        allow_reception_run=True,
    )
    second = FakeAgent(id="second", reception_output="done", allow_reception_run=True)
    im = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=False),
            agents=[
                AgentModelConfig(agent="other", model="test"),
                AgentModelConfig(agent="second", model="test"),
            ],
        )
    )
    conversations = FakeConversationManager()
    target = _source_target(address)

    result = await _run(
        React(),
        state=ReflexState(source_target=target, target=target, decision=_summon()),
        deps=ReflexDeps(
            workspaces=RecordingWorkspaceManager(),
            gateway=GatewayManager(),
            channels={"im": im},
            agents={"other": entry, "second": second},
            conversation_manager=conversations,
            thread_manager=FakeThreadManager(),
            action_manager=cast(DeferredActionManager, FakeActionManager()),
        ),
    )

    assert not isinstance(result, DeferredResult)
    assert isinstance(result.decision, SummonDecision)
    assert result.decision.agent_id == "second"
    assert [turn.prompt for turn in second.turns] == ["Please debug this in reception."]
    assert im.sub_threads[0][1] == "Working on it"


async def test_summon_here_takes_over_current_conversation() -> None:
    # A `here` summon materializes no new surface: the summoned agent runs in the
    # current conversation (Handoff's here branch).
    address = _key()
    entry = FakeAgent(
        id="other",
        reception_summon=_summon(agent_id="second", destination=HereLanding()),
        allow_reception_run=True,
    )
    second = FakeAgent(
        id="second", reception_output="took over", allow_reception_run=True
    )
    im = FakeChannelTentacle(config=_two_reception_config(stream=False))
    target = _source_target(address)

    result = await _run(
        React(),
        state=ReflexState(
            source_target=target,
            target=target,
            decision=_summon(),
            thread=_thread(address),
        ),
        deps=_summon_deps(im, entry, second),
    )

    assert not isinstance(result, DeferredResult)
    assert im.sub_threads == []
    assert second.turns[0].address == address


def _group_key(thread_id: str = "") -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="im",
        chat_type="group",
        chat_id="team",
        user_id="alice",
        channel_thread_id=thread_id,
        shared=True,
    )


def _private_key(thread_id: str = "") -> ChannelAddress:
    """A Slack assistant pane: a thread by type, readable by one person. `_key` is
    the DM around it — this is the surface whose privacy the type cannot state."""
    return ChannelAddress(
        channel_tentacle_id="im",
        chat_type="thread",
        chat_id="alice",
        user_id="alice",
        channel_thread_id=thread_id,
    )


async def _gate_for(
    address: ChannelAddress,
    channel: FakeChannelTentacle | None = None,
) -> GatewayCapability:
    agent = FakeAgent(id="other", allow_reception_run=True, reception_output="done")
    im = channel or FakeChannelTentacle(config=_two_reception_config(stream=False))
    target = _source_target(address)
    await _run(
        React(),
        state=ReflexState(source_target=target, target=target, decision=_summon()),
        deps=_deps(
            conversations=FakeConversationManager(),
            channels={"im": im},
            agent=agent,
        ),
    )
    return _recorded_gate_capability(agent.turns[0])


async def test_scheme_is_reachable_only_from_a_group_with_an_asker() -> None:
    # The reason travels with the refusal, so the model is told which of the three
    # walls it hit. None of it reaches the tool schema — see test_gateway.
    assert (await _gate_for(_group_key())).session.private_blocked_by is None
    assert (await _gate_for(_group_key("in-thread"))).session.private_blocked_by is None

    # Already one-to-one with this person, thread inside it or not.
    assert (await _gate_for(_key())).session.private_blocked_by == "already_private"
    private = await _gate_for(_private_key("assistant"))
    assert private.session.private_blocked_by == "already_private"

    # Nobody in particular asked (a scheduled or awake run).
    anonymous = replace(_group_key(), user_id="")
    assert (await _gate_for(anonymous)).session.private_blocked_by == "no_user"

    class NoDmChannel(FakeChannelTentacle):
        surfaces: ClassVar[ChannelSurfaces] = ChannelSurfaces(sub_thread=True)

    without = NoDmChannel(config=_two_reception_config(stream=False))
    assert (
        await _gate_for(_group_key(), without)
    ).session.private_blocked_by == "no_surface"


async def test_scheme_hands_the_brief_to_the_dms_own_owner() -> None:
    address = _group_key()
    entry = FakeAgent(
        id="other",
        reception_output="See you in your DMs!",
        reception_scheme=SchemeDecision(
            hint="Picking this up with you.",
            brief="Finish the migration write-up.",
            destination=ChannelAddress(
                channel_tentacle_id="im",
                chat_type="dm",
                chat_id="",
                user_id="alice",
            ),
        ),
        allow_reception_run=True,
    )
    second = FakeAgent(id="second", reception_output="on it", allow_reception_run=True)
    im = FakeChannelTentacle(config=_two_reception_config(stream=False))
    threads = FakeThreadManager()
    # alice's DM already belongs to `second`; the group cannot change that.
    dm_thread = await threads.ensure(
        ChannelAddress(
            channel_tentacle_id="im",
            chat_type="dm",
            chat_id="alice",
            user_id="alice",
        )
    )
    await threads.record_handoff(dm_thread, to_agent_tentacle_id="second")
    deps = _summon_deps(im, entry, second)
    deps.thread_manager = threads
    target = _source_target(address)

    result = await _run(
        React(),
        state=ReflexState(
            source_target=target,
            target=target,
            decision=_summon(),
            thread=_thread(address),
        ),
        deps=deps,
    )

    assert not isinstance(result, DeferredResult)
    assert im.opened_dms == ["alice"]
    # The DM's own owner picked it up, with the brief as its prompt.
    assert second.turns[0].prompt == "Finish the migration write-up."
    assert second.turns[0].address.chat_type == "dm"
    # The hint opened the far side, not the brief: a brief is written for whoever
    # answers, and a channel that can only be run inside a thread would otherwise
    # hang that thread off a prompt the person was never meant to read.
    assert im.dm_openers == ["Picking this up with you."]
    # The group gets the agent's own sign-off and nothing after it: `scheme` is an
    # ordinary tool call, so the run carries on and closes this out itself. Only the
    # group is read — the DM is on this same fake channel, and what lands there is
    # the receiving run.
    group = [sent[2][0]["text"] for sent in im.recording_ink.sent if sent[0] == "team"]
    assert group == ["See you in your DMs!"]


async def test_scheme_hands_to_the_channel_default_when_the_dm_is_unowned() -> None:
    address = _group_key()
    entry = FakeAgent(
        id="other",
        reception_scheme=SchemeDecision(
            hint="Picking this up with you.",
            brief="Do it.",
            destination=ChannelAddress(
                channel_tentacle_id="im",
                chat_type="dm",
                chat_id="",
                user_id="alice",
            ),
        ),
        allow_reception_run=True,
    )
    second = FakeAgent(id="second", reception_output="done", allow_reception_run=True)
    im = FakeChannelTentacle(config=_two_reception_config(stream=False))
    target = _source_target(address)

    result = await _run(
        React(),
        state=ReflexState(
            source_target=target,
            target=target,
            decision=_summon(),
            thread=_thread(address),
        ),
        deps=_summon_deps(im, entry, second),
    )

    assert not isinstance(result, DeferredResult)
    # No owner to defer to, so the channel's first configured agent takes it.
    assert entry.turns[-1].prompt == "Do it."
    assert entry.turns[-1].address.chat_type == "dm"


async def test_scheme_across_channels_hands_to_an_agent_that_runs_there(
    in_memory_engine: None,
) -> None:
    """`scheme` picks the receiver from the DM's own thread, resolved against the
    channel that DM is on. `im` does not run `second` at all, so a handoff resolved
    against the origin instead would land back on `im`'s own first agent — with the
    brief delivered somewhere nobody chose."""
    users = UserManager(
        {
            "luhui": UserConfig.model_validate(
                {
                    "profiles": {
                        "im": {"channel_user_id": "alice"},
                        "far": {"channel_user_id": "ou_alice"},
                    }
                }
            )
        }
    )
    await users.reconcile()
    address = _group_key()
    entry = FakeAgent(
        id="other",
        reception_scheme=SchemeDecision(
            hint="Picking this up with you.",
            brief="Finish the migration write-up.",
            destination=ChannelAddress(
                channel_tentacle_id="far",
                chat_type="dm",
                chat_id="",
                user_id="ou_alice",
            ),
        ),
        allow_reception_run=True,
    )
    second = FakeAgent(id="second", reception_output="on it", allow_reception_run=True)
    im = _channel(stream=False)  # runs `other` only
    far = FakeChannelTentacle(
        id="far",
        config=ChannelConfig(
            type="fake", agents=[AgentModelConfig(agent="second", model="test")]
        ),
    )
    deps = _summon_deps(im, entry, second, far)
    deps.thread_manager = FakeThreadManager(users=users)
    target = _source_target(address)

    result = await _run(
        React(),
        state=ReflexState(
            source_target=target,
            target=target,
            decision=_summon(),
            thread=_thread(address),
            user_profile=await users.ensure_profile(
                "im", UserProfile(channel_user_id="alice")
            ),
        ),
        deps=deps,
    )

    assert not isinstance(result, DeferredResult)
    assert far.opened_dms == ["ou_alice"]
    assert [turn.prompt for turn in second.turns] == ["Finish the migration write-up."]
    assert second.turns[0].address.channel_tentacle_id == "far"


async def test_scheme_leaves_the_turn_in_place_when_no_dm_opens() -> None:
    address = _group_key()
    entry = FakeAgent(
        id="other",
        reception_scheme=SchemeDecision(
            hint="Picking this up with you.",
            brief="Do it.",
            destination=ChannelAddress(
                channel_tentacle_id="im",
                chat_type="dm",
                chat_id="",
                user_id="alice",
            ),
        ),
        allow_reception_run=True,
    )
    second = FakeAgent(id="second", reception_output="done", allow_reception_run=True)
    im = FakeChannelTentacle(
        ink=RecordingInk(dm_opens=False),
        config=_two_reception_config(stream=False),
    )
    target = _source_target(address)

    result = await _run(
        React(),
        state=ReflexState(
            source_target=target,
            target=target,
            decision=_summon(),
            thread=_thread(address),
        ),
        deps=_summon_deps(im, entry, second),
    )

    # The platform refused at the moment of asking: nothing moved, nobody was handed
    # anything, and the origin agent's own reply already landed.
    assert not isinstance(result, DeferredResult)
    assert im.opened_dms == ["alice"]
    assert result.target.address == address
    assert second.turns == []


async def test_summon_thread_falls_back_to_main_on_sub_thread_failure() -> None:
    class FailingSubThreadChannel(FakeChannelTentacle):
        async def start_sub_thread(
            self, address: ChannelAddress, hint_text: str
        ) -> ChannelAddress:
            raise RuntimeError("platform refused the thread")

    address = _key()
    entry = FakeAgent(
        id="other",
        reception_summon=_summon(agent_id="second", destination=ThreadLanding()),
        allow_reception_run=True,
    )
    second = FakeAgent(id="second", reception_output="done", allow_reception_run=True)
    im = FailingSubThreadChannel(config=_two_reception_config(stream=False))
    target = _source_target(address)

    result = await _run(
        React(),
        state=ReflexState(
            source_target=target,
            target=target,
            decision=_summon(),
            thread=_thread(address),
        ),
        deps=_summon_deps(im, entry, second),
    )

    assert not isinstance(result, DeferredResult)
    assert result.target.mode == "main"
    assert second.turns[0].address == address


async def test_summon_thread_leaves_a_group_main_unclaimed_when_the_open_fails() -> (
    None
):
    """The same failure one surface out. Handing over on a group's main channel pins
    an owner there, and the ingest gate then answers every later message from anyone
    without a mention — which is what `allow_here` refuses at the gate. A failed open
    must not reach it by the back door, so the turn stays where it is."""

    class FailingSubThreadChannel(FakeChannelTentacle):
        async def start_sub_thread(
            self, address: ChannelAddress, hint_text: str
        ) -> ChannelAddress:
            # Both inks swallow their own send failures, so `start_sub_thread` hands
            # back the address it was given rather than raising. That is the shape
            # the node has to recognise.
            return address

    address = _group_key()
    entry = FakeAgent(
        id="other",
        reception_summon=_summon(agent_id="second", destination=ThreadLanding()),
        allow_reception_run=True,
    )
    second = FakeAgent(id="second", reception_output="done", allow_reception_run=True)
    im = FailingSubThreadChannel(config=_two_reception_config(stream=False))
    target = _source_target(address)
    thread = _thread(address)

    result = await _run(
        React(),
        state=ReflexState(
            source_target=target,
            target=target,
            decision=_summon(),
            thread=thread,
        ),
        deps=_summon_deps(im, entry, second),
    )

    assert not isinstance(result, DeferredResult)
    assert result.target.address == address
    assert second.turns == []
    assert thread.active_agent_tentacle_id is None


async def _crossing_state(
    im: FakeChannelTentacle,
) -> tuple[ReflexState, ReflexDeps, FakeChannelTentacle, FakeAgent, FakeAgent]:
    """A group main on `im` whose asker is also registered on `far`, mid-summon.

    The registry is the real one: a crossing exists because two accounts are linked,
    and the gate the entry agent calls resolves the handle through it. `far` runs
    `second` and nothing else, which is what makes the handoff land on the agent the
    summon named rather than on whatever `im` happens to list first.
    """
    users = UserManager(
        {
            "luhui": UserConfig.model_validate(
                {
                    "profiles": {
                        "im": {"channel_user_id": "alice"},
                        "far": {"channel_user_id": "ou_alice"},
                    }
                }
            )
        }
    )
    await users.reconcile()
    address = _group_key()
    far_landing = CrossingLanding(
        address=ChannelAddress(
            channel_tentacle_id="far",
            chat_type="dm",
            chat_id="",
            user_id="ou_alice",
        )
    )
    entry = FakeAgent(
        id="other",
        reception_summon=_summon(agent_id="second", destination=far_landing),
        allow_reception_run=True,
    )
    second = FakeAgent(id="second", reception_output="done", allow_reception_run=True)
    far = FakeChannelTentacle(
        id="far",
        config=ChannelConfig(
            type="fake", agents=[AgentModelConfig(agent="second", model="test")]
        ),
    )
    deps = _summon_deps(im, entry, second, far)
    deps.thread_manager = FakeThreadManager(users=users)
    target = _source_target(address)
    state = ReflexState(
        source_target=target,
        target=target,
        decision=_summon(),
        thread=_thread(address),
        user_profile=await users.ensure_profile(
            "im", UserProfile(channel_user_id="alice")
        ),
    )
    return state, deps, far, entry, second


async def test_summon_crosses_into_a_sub_thread_of_their_dms_elsewhere(
    in_memory_engine: None,
) -> None:
    # Serves the entry agent and nobody else. The summoned one is routable only
    # on `far`, which is the whole point: crossing reaches an agent this channel
    # does not run, and the handoff has to resolve against the one it lands on.
    im = _channel(stream=False)
    state, deps, far, _entry, second = await _crossing_state(im)

    result = await _run(React(), state=state, deps=deps)

    assert not isinstance(result, DeferredResult)
    # Their direct messages there had to be opened before there was anywhere to
    # open a sub-thread of, and the sub-thread is what the turn actually lands in.
    assert far.opened_dms == ["ou_alice"]
    assert [address for address, _hint in far.sub_threads] == [
        ChannelAddress(
            channel_tentacle_id="far",
            chat_type="dm",
            chat_id="ou_alice",
            user_id="ou_alice",
        )
    ]
    landed = second.turns[0].address
    assert landed.channel_tentacle_id == "far"
    assert landed.channel_thread_id == "hint-thread"
    assert second.turns[0].prompt == "Please debug this in reception."
    # The group is told, or it watches the conversation leave without a word.
    assert im.recording_ink.sent[-1][2][0]["text"] == "Working on it"


async def test_a_crossing_that_opens_no_sub_thread_leaves_the_dms_unclaimed(
    in_memory_engine: None,
) -> None:
    """The direct messages open but the sub-thread does not. Landing on the direct
    messages themselves would pin an agent the *group* chose onto this person's
    private conversation — the one thing `scheme` exists to route around — so the
    turn stays where it is instead."""

    class NoSubThreadOpens(FakeChannelTentacle):
        async def start_sub_thread(
            self, address: ChannelAddress, hint_text: str
        ) -> ChannelAddress:
            return address

    # Serves the entry agent and nobody else. The summoned one is routable only
    # on `far`, which is the whole point: crossing reaches an agent this channel
    # does not run, and the handoff has to resolve against the one it lands on.
    im = _channel(stream=False)
    state, deps, _far, _entry, second = await _crossing_state(im)
    deps.channels["far"] = NoSubThreadOpens(
        id="far",
        config=ChannelConfig(
            type="fake", agents=[AgentModelConfig(agent="second", model="test")]
        ),
    )

    result = await _run(React(), state=state, deps=deps)

    assert not isinstance(result, DeferredResult)
    assert result.target.address == _group_key()
    assert second.turns == []


async def test_a_crossing_stays_put_when_the_far_dm_never_opens(
    in_memory_engine: None,
) -> None:
    # Serves the entry agent and nobody else. The summoned one is routable only
    # on `far`, which is the whole point: crossing reaches an agent this channel
    # does not run, and the handoff has to resolve against the one it lands on.
    im = _channel(stream=False)
    state, deps, _far, _entry, second = await _crossing_state(im)
    deps.channels["far"] = FakeChannelTentacle(
        id="far",
        ink=RecordingInk(dm_opens=False),
        config=ChannelConfig(
            type="fake", agents=[AgentModelConfig(agent="second", model="test")]
        ),
    )

    result = await _run(React(), state=state, deps=deps)

    # The platform refused as it was asked, so nothing moved and the origin agent's
    # own reply is all that landed.
    assert not isinstance(result, DeferredResult)
    assert result.target.address == _group_key()
    assert second.turns == []


async def test_a_native_summon_signal_crosses_and_hands_off(
    in_memory_engine: None,
) -> None:
    """Awake meets a native session's summon where React meets a driven one's: the
    crossing opens on the far channel, the handoff row says from=claude-native,
    and the brief is the far agent's prompt. The source is the native
    pseudo-channel nobody serves, which the crossing never needs to look up."""
    users = UserManager(
        {
            "luhui": UserConfig.model_validate(
                {
                    "profiles": {
                        CLAUDE_NATIVE_ID: {"channel_user_id": "native"},
                        "far": {"channel_user_id": "ou_alice"},
                    }
                }
            )
        }
    )
    await users.reconcile()
    second = FakeAgent(id="second", reception_output="done", allow_reception_run=True)
    far = FakeChannelTentacle(
        id="far",
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=False),
            agents=[AgentModelConfig(agent="second", model="test")],
        ),
    )
    deps = ReflexDeps(
        channels={"far": far},
        agents={"second": second},
        workspaces=RecordingWorkspaceManager(),
        conversation_manager=FakeConversationManager(),
        thread_manager=FakeThreadManager(users=users),
        action_manager=cast(DeferredActionManager, FakeActionManager()),
        gateway=GatewayManager(),
    )
    signal = GatewayHandoffSignal(
        decision=SummonDecision(
            action="summon",
            agent_id="second",
            model="test",
            destination=CrossingLanding(
                address=ChannelAddress(
                    channel_tentacle_id="far",
                    chat_type="dm",
                    chat_id="",
                    user_id="ou_alice",
                )
            ),
            reason="needs work",
            hint="Working on it",
            summon="Please take this up over here.",
        ),
        agent_id=CLAUDE_NATIVE_ID,
        user_profile=await users.profile(CLAUDE_NATIVE_ID, "native"),
        source=ChannelAddress(
            channel_tentacle_id=CLAUDE_NATIVE_ID,
            chat_type="dm",
            chat_id="",
            user_id="native",
        ),
    )

    result = await _run(Awake(signal=signal), state=ReflexState(), deps=deps)

    assert not isinstance(result, DeferredResult)
    assert far.opened_dms == ["ou_alice"]
    landed = second.turns[0].address
    assert landed.channel_tentacle_id == "far"
    assert landed.channel_thread_id == "hint-thread"
    assert second.turns[0].prompt == "Please take this up over here."
    threads = deps.thread_manager
    assert isinstance(threads, FakeThreadManager)
    [handoff] = threads.handoffs
    assert handoff.from_agent_tentacle_id == CLAUDE_NATIVE_ID
    assert handoff.to_agent_tentacle_id == "second"
    assert handoff.brief == "Please take this up over here."


async def test_teleport_carries_the_history_across_to_a_far_sub_thread(
    in_memory_engine: None,
) -> None:
    """The same agent, one channel over. A teleport takes its whole history with it,
    so the fork is what has to land there — not a fresh conversation."""
    address = _key()  # A DM: nobody else's words travel with this.
    entry = FakeAgent(
        id="other",
        reception_teleport="carrying on over there",
        reception_teleport_destination="far",
        reception_output="continued",
        allow_reception_run=True,
    )
    second = FakeAgent(id="second", reception_output="unused")
    im = _channel(stream=False)
    far = FakeChannelTentacle(
        id="far",
        config=ChannelConfig(
            type="fake", agents=[AgentModelConfig(agent="other", model="test")]
        ),
    )
    target = _source_target(address)

    result = await _run(
        React(),
        state=ReflexState(
            source_target=target,
            target=target,
            decision=_summon(),
            thread=_thread(address),
        ),
        deps=_summon_deps(im, entry, second, far),
    )

    assert not isinstance(result, DeferredResult)
    # Their direct messages there, then a sub-thread inside them — and the agent
    # resumed against the fork in it.
    assert far.opened_dms == ["ou_alice"]
    landed = entry.turns[-1].address
    assert landed.channel_tentacle_id == "far"
    assert landed.channel_thread_id == "hint-thread"
    # And the origin was told, since a crossing posts nothing where it came from.
    assert im.recording_ink.sent[-1][2][0]["text"] == "carrying on over there"


async def test_a_teleport_crossing_that_never_opens_resolves_in_place() -> None:
    address = _key()
    entry = FakeAgent(
        id="other",
        reception_teleport="carrying on over there",
        reception_teleport_destination="far",
        reception_output="stayed here",
        allow_reception_run=True,
    )
    second = FakeAgent(id="second", reception_output="unused")
    im = _channel(stream=False)
    far = FakeChannelTentacle(
        id="far",
        ink=RecordingInk(dm_opens=False),
        config=ChannelConfig(
            type="fake", agents=[AgentModelConfig(agent="other", model="test")]
        ),
    )
    target = _source_target(address)

    result = await _run(
        React(),
        state=ReflexState(
            source_target=target,
            target=target,
            decision=_summon(),
            thread=_thread(address),
        ),
        deps=_summon_deps(im, entry, second, far),
    )

    # The deferral still has to be resolved or the run hangs on it: stay put and
    # answer here, with nothing forked anywhere.
    assert not isinstance(result, DeferredResult)
    assert entry.turns[-1].address == address
    assert far.sub_threads == []


async def test_a_recorded_teleport_moves_the_turn_after_it_ends() -> None:
    """A runtime that cannot be suspended mid-run reports a teleport as a decision;
    the graph performs the same move it makes for a deferral, and the resumed run
    opens from the hint rather than from a resolved call."""
    address = _key()
    entry = FakeAgent(
        id="other",
        reception_recorded_teleport="carrying on in a thread",
        reception_output="continued",
        allow_reception_run=True,
    )
    second = FakeAgent(id="second", reception_output="unused")
    im = _channel(stream=False)
    target = _source_target(address)

    result = await _run(
        React(),
        state=ReflexState(
            source_target=target,
            target=target,
            decision=_summon(),
            thread=_thread(address),
        ),
        deps=_summon_deps(im, entry, second),
    )

    assert not isinstance(result, DeferredResult)
    assert len(entry.turns) == 2
    resumed = entry.turns[-1]
    # No pending call to resolve — the hint is the resumed run's opening prompt.
    assert resumed.deferred_results is None
    assert resumed.prompt == "carrying on in a thread"
    assert resumed.address.channel_thread_id == "hint-thread"


async def test_reception_returns_deferred_result_on_human_question() -> None:
    address = _key()
    requests = _requests()
    decision = _summon()
    agent = FakeAgent(id="other", reception_output=requests)
    conversations = FakeConversationManager()
    action_manager = FakeActionManager(
        presented_batch=FakePresentedBatch(
            questions=[
                DeferredQuestion(
                    tool_name="ask_questions",
                    tool_call_id="call_question",
                    position=0,
                    args={"question": "What should I clarify?"},
                    metadata={},
                )
            ]
        )
    )
    im = _channel(stream=True)
    target = _source_target(address)

    result = await _run(
        React(),
        state=ReflexState(
            source_target=target,
            target=target,
            decision=decision,
            thread=_thread(address),
        ),
        deps=_deps(
            conversations=conversations,
            channels={"im": im},
            agent=agent,
            action_manager=action_manager,
        ),
    )

    assert isinstance(result, DeferredResult)
    assert result.run_name == "react"
    assert result.requests is requests
    assert result.batch_id == action_manager.create_calls[0].batch_id


async def test_reception_fails_fast_when_stream_produces_no_result() -> None:
    address = _key()
    agent = FakeAgent(id="other", reception_output="done")
    conversations = FakeConversationManager()
    im = DroppingChannel(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=True),
            agents=[AgentModelConfig(agent="other", model="test")],
        )
    )
    target = _source_target(address)

    with pytest.raises(RuntimeError, match="completed without a result"):
        await _run(
            React(),
            state=ReflexState(source_target=target, target=target, decision=_summon()),
            deps=_deps(conversations=conversations, channels={"im": im}, agent=agent),
        )


async def test_route_runs_in_place_inside_flat_thread() -> None:
    address = _key(thread_id="existing-thread")
    agent = FakeAgent(id="other", reception_output="done")
    conversations = FakeConversationManager()
    im = _channel()

    result = await _run(
        Route(),
        state=_state(address, user_prompt="continue"),
        deps=_deps(conversations=conversations, channels={"im": im}, agent=agent),
    )

    assert not isinstance(result, DeferredResult)
    assert result.target.mode == "sub"
    assert agent.streams[0].address == address
    assert agent.streams[0].run_name == "react"
    assert im.sub_threads == []
    assert im.consumed[0][0] == address


async def test_awake_short_circuits_on_empty_signal() -> None:
    agent = FakeAgent()
    conversations = FakeConversationManager()
    im = _channel()

    result = await _run(
        Awake(signal=UserMessageSignal([])),
        state=ReflexState(),
        deps=_deps(conversations=conversations, channels={"im": im}, agent=agent),
    )

    assert not isinstance(result, DeferredResult)
    assert result.decision is None
    assert agent.turns == []
    assert im.sent == []


async def test_awake_short_circuits_on_empty_prompt() -> None:
    class BlankEvent(MessageEvent):
        def __str__(self) -> str:
            return "   "

    agent = FakeAgent()
    conversations = FakeConversationManager()
    im = _channel()
    event = BlankEvent(
        tentacle_id="im",
        message_id="m1",
        chat_type="dm",
        chat_id="alice",
        user_id="alice",
        segments=[],
    )

    result = await _run(
        Awake(signal=UserMessageSignal([event])),
        state=ReflexState(),
        deps=_deps(conversations=conversations, channels={"im": im}, agent=agent),
    )

    assert not isinstance(result, DeferredResult)
    assert result.decision is None
    assert agent.turns == []
    assert im.sent == []


async def test_awake_raises_for_unknown_channel_or_agent() -> None:
    agent = FakeAgent()
    conversations = FakeConversationManager()
    im = _channel()
    event = MessageEvent(
        tentacle_id="ghost",
        message_id="m1",
        chat_type="dm",
        chat_id="alice",
        user_id="alice",
        segments=[TextSegment(data={"text": "hi"})],
    )

    with pytest.raises(ValueError, match="unknown channel"):
        await _run(
            Awake(signal=UserMessageSignal([event])),
            state=ReflexState(),
            deps=_deps(conversations=conversations, channels={"im": im}, agent=agent),
        )


async def test_resume_routes_reception_batch_to_run_reception() -> None:
    address = _key(thread_id="hint-thread")
    deferred_results = _deferred_results()
    batch = FakeDeferredBatch(
        source_address=_key(),
        target_address=address,
        requests=_requests(),
        deferred_results=deferred_results,
        run_name="react",
        target_mode="sub",
    )
    agent = FakeAgent(id="other", reception_output="resumed answer")
    conversations = FakeConversationManager()
    action_manager = FakeActionManager(batch=batch)
    im = _channel()

    result = await _run(
        ResumeDeferred(awake=DeferredActionBatchResponse(batch_id=batch.id)),
        state=ReflexState(),
        deps=_deps(
            conversations=conversations,
            channels={"im": im},
            agent=agent,
            action_manager=action_manager,
        ),
    )

    assert not isinstance(result, DeferredResult)
    assert result.result is not None
    assert result.result.output == "resumed answer"
    assert agent.streams[0].prompt is None
    assert agent.streams[0].address == address
    # The resumed run must carry the batch conversation's thread; without it the
    # agent run fails fast ("requires a thread_id to own its conversation").
    assert agent.streams[0].thread_id is not None
    assert [stream.deferred_results for stream in agent.streams] == [deferred_results]
    assert action_manager.marked == [
        (batch.id, "resuming", False),
        (batch.id, "completed", True),
    ]


@dataclass
class ProfileRecordingAgent(FakeAgent):
    bound_profiles: list[UserProfile] = field(default_factory=list)

    async def user_capabilities(
        self, profile: UserProfile
    ) -> list[AgentCapability[None]]:
        self.bound_profiles.append(profile)
        return []


async def test_resume_rebinds_the_suspended_run_user() -> None:
    """A resumed run serves the user the suspended run served: ResumeDeferred
    resolves their profile from the batch's source address, so React mounts the
    same per-user capabilities instead of silently running without them."""
    address = _key(thread_id="hint-thread")
    batch = FakeDeferredBatch(
        source_address=_key(),
        target_address=address,
        requests=_requests(),
        deferred_results=_deferred_results(),
        run_name="react",
        target_mode="sub",
    )
    agent = ProfileRecordingAgent(id="other", reception_output="resumed answer")
    deps = _deps(
        conversations=FakeConversationManager(),
        channels={"im": _channel()},
        agent=agent,
        action_manager=FakeActionManager(batch=batch),
    )
    profile = UserProfile(channel_tentacle_id="im", channel_user_id="alice")
    cast(FakeUserManager, deps.thread_manager.users).profiles[("im", "alice")] = profile

    result = await _run(
        ResumeDeferred(awake=DeferredActionBatchResponse(batch_id=batch.id)),
        state=ReflexState(),
        deps=deps,
    )

    assert not isinstance(result, DeferredResult)
    assert agent.bound_profiles == [profile]


async def test_resume_returns_result_for_already_completed_batch() -> None:
    address = _key(thread_id="hint-thread")
    decision = SummonDecision(
        action="summon",
        agent_id="inkling",
        model="test",
        reason="resumed",
        hint="resumed",
        summon="",
    )
    batch = FakeDeferredBatch(
        source_address=_key(),
        target_address=address,
        requests=_requests(),
        deferred_results=_deferred_results(),
        run_name="react",
        target_mode="sub",
        decision=decision,
        status="completed",
    )
    agent = FakeAgent()
    conversations = FakeConversationManager()
    action_manager = FakeActionManager(batch=batch)
    im = _channel()

    result = await _run(
        ResumeDeferred(awake=DeferredActionBatchResponse(batch_id=batch.id)),
        state=ReflexState(),
        deps=_deps(
            conversations=conversations,
            channels={"im": im},
            agent=agent,
            action_manager=action_manager,
        ),
    )

    assert not isinstance(result, DeferredResult)
    assert result.decision == decision
    assert agent.streams == []
    assert action_manager.marked == []


async def test_resume_keeps_incomplete_reception_batch_deferred() -> None:
    address = _key(thread_id="hint-thread")
    batch = FakeDeferredBatch(
        source_address=_key(),
        target_address=address,
        requests=_requests(),
        deferred_results=_deferred_results(),
        run_name="react",
        target_mode="sub",
        status="pending",
        completed=False,
    )
    agent = FakeAgent()
    conversations = FakeConversationManager()
    action_manager = FakeActionManager(batch=batch)
    im = _channel()

    result = await _run(
        ResumeDeferred(awake=DeferredActionBatchResponse(batch_id=batch.id)),
        state=ReflexState(),
        deps=_deps(
            conversations=conversations,
            channels={"im": im},
            agent=agent,
            action_manager=action_manager,
        ),
    )

    assert isinstance(result, DeferredResult)
    assert result.run_name == "react"
    assert result.requests is batch.requests
    assert agent.streams == []
    assert action_manager.marked == []


async def _run_send(
    im: FakeChannelTentacle,
    destination: ChannelAddress | None,
) -> tuple[FakeThreadManager, FakeChannelTentacle]:
    """One reception whose only act is a `send` for `destination`."""
    address = _group_key()
    agent = FakeAgent(
        id="other",
        allow_reception_run=True,
        reception_script=[
            MessageSentEvent(
                segments=[MarkdownSegment(data={"text": "the summary"})],
                destination=destination,
            ),
            AgentRunResultEvent(AgentRunResult("sent it over")),
        ],
    )
    threads = FakeThreadManager()
    deps = _deps(
        conversations=FakeConversationManager(), channels={"im": im}, agent=agent
    )
    deps.thread_manager = threads
    target = _source_target(address)
    await _run(
        React(),
        state=ReflexState(
            source_target=target,
            target=target,
            decision=_summon(),
            thread=_thread(address),
        ),
        deps=deps,
    )
    return threads, im


async def test_send_to_dm_delivers_privately_and_leaves_the_group_alone() -> None:
    # No handoff: the DM gets a bare message, and this run keeps the conversation.
    threads, im = await _run_send(
        _channel(stream=True),
        ChannelAddress(
            channel_tentacle_id="im",
            chat_type="dm",
            chat_id="",
            user_id="alice",
        ),
    )

    assert im.opened_dms == ["alice"]
    delivered = [chat_id for chat_id, *_ in im.sent]
    assert "alice" in delivered

    # It is on the DM's ledger — so whoever handles that DM meets it as pending
    # context next turn — while no conversation's model messages were touched.
    dm_thread = threads.threads_by_key[
        ThreadKey.from_address(
            ChannelAddress(
                channel_tentacle_id="im",
                chat_type="dm",
                chat_id="alice",
                user_id="alice",
            )
        )
    ]
    dm_rows = [
        message for message in threads.outbounds if message.thread_id == dm_thread.id
    ]
    assert [row.message_text for row in dm_rows] == ["the summary"]


async def test_send_here_is_left_for_the_timeline() -> None:
    # `destination` is None for this conversation, so nothing is diverted and the
    # timeline renders it the way it renders any other mid-run notice.
    _threads, im = await _run_send(_channel(stream=True), None)

    assert im.opened_dms == []
    assert all(chat_id == "team" for chat_id, *_ in im.sent)


async def test_send_falls_back_to_here_when_the_platform_will_not_open() -> None:
    # The gate refuses a surface it knows is unreachable, so what is left here is the
    # platform failing at the moment of asking. Content produced for this user then
    # belongs in the conversation they asked from rather than nowhere.
    im = FakeChannelTentacle(
        ink=RecordingInk(dm_opens=False),
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=True),
            agents=[AgentModelConfig(agent="other", model="test")],
        ),
    )
    _threads, im = await _run_send(
        im,
        ChannelAddress(
            channel_tentacle_id="im",
            chat_type="dm",
            chat_id="",
            user_id="alice",
        ),
    )

    assert im.opened_dms == ["alice"]
    assert all(chat_id == "team" for chat_id, *_ in im.sent)
