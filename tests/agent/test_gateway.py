from __future__ import annotations

from dataclasses import asdict
from typing import ClassVar, Literal, cast
from unittest.mock import AsyncMock

import pytest
from octomate_protocol.gateway import GatewayTool
from pydantic import TypeAdapter, ValidationError
from pydantic_ai import CallDeferred, RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.settings import ThinkingEffort
from uuid_utils.compat import uuid7

from octomate.capabilities.gateway import GatewayCapability
from octomate.config import ChannelConfig
from octomate.config.agents import AgentRouteModelName
from octomate.managers.gateway import (
    GatewayManager,
    GatewayRefusal,
    OctomateSession,
    PrivateBlocker,
)
from octomate.managers.thread import ThreadManager
from octomate.managers.user import UserManager
from octomate.schemas.conversation import ChannelAddress, ChatType
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import ThreadKey
from octomate.schemas.triage import (
    AgentRoute,
    Claim,
    SchemeDecision,
    SummonDecision,
    TeleportDecision,
)
from octomate.schemas.user import UserProfile
from octomate.tentacles.channel import ChannelSurfaces
from octomate.types.threads import CLAUDE_NATIVE_ID, CODEX_NATIVE_ID
from tests.support.agents import FakeAgent
from tests.support.channels import FakeChannelTentacle
from tests.support.users import a_user

FAKE_CONTEXT = cast(RunContext[None], None)


CLAUDE_CLAIM = Claim(ability="coding work", efforts=("medium", "high"))
CLAUDE_ROUTE = AgentRoute(agent_id="claude", model="opus", claim=CLAUDE_CLAIM)
INKLING_ROUTE = AgentRoute(
    agent_id="inkling",
    model="deepseek:deepseek-chat",
    claim=Claim(ability="current agent", efforts=("low", "medium", "high")),
)


class _NoDmChannel(FakeChannelTentacle):
    surfaces: ClassVar[ChannelSurfaces] = ChannelSurfaces(sub_thread=True)


class _NoSubThreadChannel(FakeChannelTentacle):
    surfaces: ClassVar[ChannelSurfaces] = ChannelSurfaces(direct_message=True)


# The four surfaces a run can sit on, as (chat_type, shared, channel_thread_id).
# They are not three free booleans: a thread overwrites the type of the chat around
# it, and a shared surface with no thread is a group's main channel. So a gate can
# never both take over in place and open a sub-thread from the same address unless
# the surface is private. A summon only ever takes over in place, so its tests below
# sit on a thread, the one shared surface that allows it.
Shape = Literal["private_main", "private_thread", "shared_main", "shared_thread"]
SHAPES: dict[Shape, tuple[ChatType, bool, str]] = {
    "private_main": ("dm", False, ""),
    "private_thread": ("thread", False, "t-1"),
    "shared_main": ("group", True, ""),
    "shared_thread": ("thread", True, "t-1"),
}


def _capability(
    shape: Shape = "shared_main",
    *,
    channel: FakeChannelTentacle | None = None,
    user_id: str = "alice",
) -> GatewayCapability:
    """A gate answering from `shape`, on a channel with every surface unless one is
    passed. `test_reflex_graph` covers how the real channels reach each shape."""
    chat_type, shared, thread_id = SHAPES[shape]
    capability = GatewayCapability(
        session=OctomateSession(
            channel_routes={"im": [CLAUDE_ROUTE, INKLING_ROUTE]},
            current_agent_id="inkling",
            channels={"im": channel or FakeChannelTentacle()},
            conversation_address=ChannelAddress(
                channel_tentacle_id="im",
                chat_type=chat_type,
                chat_id="room",
                channel_thread_id=thread_id,
                user_id=user_id,
                shared=shared,
            ),
        )
    )
    return capability


def _blocked(reason: PrivateBlocker) -> GatewayCapability:
    """A gate whose `scheme` has nowhere to land, each wall reached by the surface
    or the channel that actually produces it rather than by asking for the wall."""
    if reason == "no_surface":
        return _capability(channel=_NoDmChannel())
    if reason == "already_private":
        return _capability("private_main")
    return _capability(user_id="")


def _decision(
    agent_id: str = "claude",
    model: AgentRouteModelName = "opus",
    effort: ThinkingEffort | None = None,
) -> SummonDecision:
    return SummonDecision(
        action="summon",
        agent_id=agent_id,
        model=model,
        effort=effort,
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )


def test_summon_decision_requires_model_field() -> None:
    with pytest.raises(ValidationError, match="model"):
        # The omission is the input under test: pyright is right that the call is
        # invalid, and that it is invalid is exactly what this asserts.
        SummonDecision(  # pyright: ignore[reportCallIssue]
            action="summon",
            agent_id="claude",
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )


def test_summon_decision_rejects_empty_model() -> None:
    with pytest.raises(ValidationError, match="model"):
        SummonDecision(
            action="summon",
            agent_id="claude",
            model="",
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )


async def test_summon_capability_accepts_exact_route() -> None:
    capability = _capability("shared_thread")
    assert capability.toolset is not None
    summon = capability.toolset.tools[GatewayTool.SUMMON].function

    await summon(
        FAKE_CONTEXT,
        agent_id="claude",
        model="opus",
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )

    assert capability.decision == _decision()


def test_gate_instruction_explains_each_spell_in_plain_words() -> None:
    instructions = _capability().get_instructions()

    assert "`inspect`" in instructions
    assert "`summon`" in instructions
    assert "`teleport`" in instructions
    assert "route the conversation" in instructions
    # Inkling's own handoff guidance: reference by handle rather than repeat.
    assert "### Writing a brief" in instructions
    assert "`#msg:<id>`" in instructions
    # No concrete route details leak into the shared instruction block.
    assert "agent_id=claude" not in instructions
    assert "coding work" not in instructions
    assert "target_id" not in instructions


async def test_inspect_tool_returns_other_routes() -> None:
    # A shared thread: the one shape that offers both built-ins at once.
    capability = _capability("shared_thread")
    assert capability.toolset is not None
    assert "inspect" in capability.toolset.tools
    assert "scry" not in capability.toolset.tools
    inspect_tool = capability.toolset.tools[GatewayTool.INSPECT].function

    routes = await inspect_tool(FAKE_CONTEXT, "routes")
    places = await inspect_tool(FAKE_CONTEXT, "destinations")

    assert routes == [
        AgentRoute(
            agent_id="claude",
            model="opus",
            claim=CLAUDE_CLAIM,
        )
    ]
    assert places == [ChannelAddress("im", "dm", "", "alice")]


async def test_inspect_computes_only_the_facet_it_was_asked_for() -> None:
    capability = _capability("shared_thread")
    assert capability.toolset is not None
    inspect_tool = capability.toolset.tools[GatewayTool.INSPECT].function

    await inspect_tool(FAKE_CONTEXT, "routes")

    # The registry was never reached for the facet nobody asked for.
    assert capability.session.destination_cache is None


async def test_destination_discovery_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    users = UserManager()
    session = OctomateSession(
        channel_routes={},
        current_agent_id="inkling",
        users=users,
        user_profile=UserProfile(channel_user_id="alice"),
    )
    discover = AsyncMock(return_value=[])
    monkeypatch.setattr(users, "linked_profiles", discover)
    discover.assert_not_called()

    first = await session.destinations()

    assert first == []
    assert await session.destinations() is first
    assert await session.inspect("destinations") == []
    await session.operations
    discover.assert_awaited_once_with(session.user_profile)


async def test_a_native_session_cannot_be_summoned() -> None:
    """A summon hands the conversation over where it is, and a native session has
    nothing here to hand over: it teleports into a thread first."""
    route = AgentRoute(agent_id="claude", model="test", claim=CLAUDE_CLAIM)
    session = OctomateSession(
        channel_routes={"far": [route]},
        current_agent_id=CODEX_NATIVE_ID,
        native=True,
        channels={"far": FakeChannelTentacle("far")},
        user_profile=UserProfile(channel_tentacle_id="far", channel_user_id="alice"),
    )

    operations = await session.operations
    assert "teleport it into a thread first" in (operations.summon.reason or "")
    with pytest.raises(GatewayRefusal, match="teleport it into a thread first"):
        await session.summon(
            agent_id="claude",
            model="test",
            hint="Continue",
            reason="replacement",
            summon="Carry on from this brief.",
        )
    assert session.decision is None


async def test_summon_capability_rejects_self_summon() -> None:
    capability = _capability()
    assert capability.toolset is not None
    summon = capability.toolset.tools[GatewayTool.SUMMON].function

    with pytest.raises(ModelRetry, match="Cannot summon yourself"):
        await summon(
            FAKE_CONTEXT,
            agent_id="inkling",
            model="opus",
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )


@pytest.mark.parametrize(
    ("agent_id", "model"),
    [
        # No such model on any route.
        ("claude", "sonnet"),
        # Both halves belong to a route, but not to the *same* one — validation is
        # pair-wise, which is why the route is matched rather than each arg checked.
        ("claude", "deepseek:deepseek-chat"),
    ],
)
async def test_summon_tool_retries_invalid_route(agent_id: str, model: str) -> None:
    capability = _capability("shared_thread")
    assert capability.toolset is not None
    summon = capability.toolset.tools[GatewayTool.SUMMON].function

    with pytest.raises(ModelRetry, match="Invalid summon route"):
        await summon(
            FAKE_CONTEXT,
            agent_id=agent_id,
            model=model,
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )


async def test_summon_carries_a_claimed_effort() -> None:
    capability = _capability("shared_thread")
    assert capability.toolset is not None
    summon = capability.toolset.tools[GatewayTool.SUMMON].function

    await summon(
        FAKE_CONTEXT,
        agent_id="claude",
        model="opus",
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
        effort="high",
    )

    assert capability.decision == _decision(effort="high")


async def test_summon_refuses_an_unclaimed_effort() -> None:
    capability = _capability("shared_thread")
    assert capability.toolset is not None
    summon = capability.toolset.tools[GatewayTool.SUMMON].function

    with pytest.raises(ModelRetry, match="does not accept effort 'low'"):
        await summon(
            FAKE_CONTEXT,
            agent_id="claude",
            model="opus",
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
            effort="low",
        )
    assert capability.decision is None


@pytest.mark.parametrize(
    ("shared", "blocked_by"),
    [(True, None), (False, "already_private")],
)
async def test_a_threads_privacy_is_read_from_its_surface_not_its_type(
    shared: bool,
    blocked_by: PrivateBlocker | None,
) -> None:
    """Both of these are `chat_type="thread"`: a thread in a group channel, and a
    Slack assistant pane or Lark p2p topic. Only one has somewhere private left to
    move to — reading the type alone would offer `scheme` a surface beside the one
    it is already in, under whatever agent owns that."""
    capability = GatewayCapability(
        session=OctomateSession(
            channel_routes={},
            current_agent_id="inkling",
            channels={"im": FakeChannelTentacle()},
            conversation_address=ChannelAddress(
                channel_tentacle_id="im",
                chat_type="thread",
                chat_id="room",
                user_id="alice",
                channel_thread_id="t-1",
                shared=shared,
            ),
        )
    )

    assert capability.session.private_blocked_by == blocked_by
    # A thread pins an owner either way, so taking one over in place is always fine.
    assert capability.session.allow_here is True


async def test_summon_here_refused_when_disallowed() -> None:
    capability = _capability("shared_main")
    assert capability.toolset is not None
    summon = capability.toolset.tools[GatewayTool.SUMMON].function

    with pytest.raises(ModelRetry, match="A group's main channel"):
        await summon(
            FAKE_CONTEXT,
            agent_id="claude",
            model="opus",
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )
    assert capability.decision is None


async def test_summon_here_allowed_on_bounded_surface() -> None:
    capability = _capability("shared_thread")
    assert capability.toolset is not None
    summon = capability.toolset.tools[GatewayTool.SUMMON].function

    await summon(
        FAKE_CONTEXT,
        agent_id="claude",
        model="opus",
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )

    assert capability.decision == _decision()


async def test_summon_tool_records_decision() -> None:
    capability = _capability("shared_thread")
    assert capability.toolset is not None
    summon = capability.toolset.tools[GatewayTool.SUMMON].function

    result = await summon(
        FAKE_CONTEXT,
        agent_id="claude",
        model="opus",
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )

    assert result == "Summoning claude (opus) to take over here."
    assert capability.decision == _decision()


async def test_teleport_defers_the_run() -> None:
    capability = _capability()
    assert capability.toolset is not None
    teleport = capability.toolset.tools[GatewayTool.TELEPORT].function

    with pytest.raises(CallDeferred):
        await teleport(FAKE_CONTEXT, hint="let's move to a thread")


@pytest.mark.parametrize(
    ("shape", "channel"),
    [
        ("private_thread", None),
        ("shared_thread", None),
        ("shared_main", _NoSubThreadChannel()),
    ],
)
async def test_teleport_refused_where_no_sub_thread_can_be_opened(
    shape: Shape,
    channel: FakeChannelTentacle | None,
) -> None:
    """Nothing nests — every channel with threads is `flat_thread` — and some open
    none at all. Refusing beats deferring into a move that never happens and
    telling the agent it relocated. Nobody is linked anywhere else here, so there is
    no crossing to fall back on and the refusal has nothing to offer instead."""
    capability = _capability(shape, channel=channel)
    assert capability.toolset is not None
    teleport = capability.toolset.tools[GatewayTool.TELEPORT].function

    with pytest.raises(
        ModelRetry, match=r"cannot contain|Only the current|no conversation can land"
    ):
        await teleport(FAKE_CONTEXT, hint="let's move to a thread")


def test_move_spells_use_addresses_and_explicit_creation_intent() -> None:
    capability = _capability()
    assert capability.toolset is not None
    schema = capability.toolset.tools[
        GatewayTool.TELEPORT
    ].tool_def.parameters_json_schema
    assert "ChannelAddress" in schema["$defs"]
    assert schema["properties"]["new_thread"]["default"] is True
    assert not any(key.endswith("Target") for key in schema["$defs"])
    # A summon takes over where it is, so it names nowhere to go.
    summon = capability.toolset.tools[
        GatewayTool.SUMMON
    ].tool_def.parameters_json_schema
    assert not {"destination", "new_thread"} & set(summon["properties"])
    assert _destination_kinds(capability, GatewayTool.SCHEME) == ["dm"]
    assert _destination_kinds(capability, GatewayTool.SEND) == ["dm", "here"]


async def _crossable(
    shape: Shape = "shared_main",
    *,
    far_channel: FakeChannelTentacle | None = None,
    far_routes: tuple[AgentRoute, ...] = (CLAUDE_ROUTE,),
) -> GatewayCapability:
    """A gate on `shape` whose asker is also registered on `far`, where `far_routes`
    run.

    The registry is the real one — a crossing exists precisely because two accounts
    are linked, and faking that link would fake the thing under test. What `far` runs
    is its own config, so the routes drive both what it advertises and who the gate
    will let a spell name there.
    """
    await a_user("luhui", profiles={"im": "alice", "far": "ou_alice"})
    users = UserManager()
    capability = _capability(shape)
    session = capability.session
    session.users = users
    session.user_profile = await users.ensure_profile(
        "im", UserProfile(channel_user_id="alice")
    )
    session.channel_routes = {
        **session.channel_routes,
        "far": list(far_routes),
    }
    session.channels = {
        "im": FakeChannelTentacle(),
        "far": far_channel
        or FakeChannelTentacle(
            id="far",
            config=ChannelConfig(
                type="fake",
                agents=[route.agent_id for route in far_routes],
            ),
        ),
    }
    session.agents = {
        route.agent_id: FakeAgent(id=route.agent_id) for route in far_routes
    }
    return capability


async def test_a_channel_that_opens_no_sub_thread_is_shown_closed(
    in_memory_engine: None,
) -> None:
    """`scheme` reaches a channel like this — it lands in the direct messages
    themselves. A teleport lands in a sub-thread of them, so there is nowhere for it
    to go: the channel is shown with why, and every way in says the same."""
    capability = await _crossable(
        "private_main",
        far_routes=(INKLING_ROUTE,),
        far_channel=_NoSubThreadChannel(
            id="far",
            config=ChannelConfig(type="fake", agents=["inkling"]),
        ),
    )
    assert capability.toolset is not None
    teleport = capability.toolset.tools[GatewayTool.TELEPORT].function
    session = capability.session
    closed = "This channel has no threads, so no conversation can land here."

    operations = await session.operations
    assert operations.barred == {"far": closed}
    assert all(
        one.channel_tentacle_id != "far" for one in operations.teleport.destinations
    )
    [far_dm] = [
        one
        for one in await session.inspect("destinations")
        if one.channel_tentacle_id == "far"
    ]
    assert far_dm.metadata == {"barred": closed}
    with pytest.raises(GatewayRefusal, match=closed):
        await session.inspect("destinations", "far")
    with pytest.raises(ModelRetry, match=closed):
        await teleport(
            FAKE_CONTEXT,
            hint="Working on it",
            destination=ChannelAddress(
                channel_tentacle_id="far",
                chat_type="dm",
                chat_id="",
                user_id="ou_alice",
            ),
        )
    assert capability.decision is None


@pytest.mark.parametrize(
    ("shape", "destinations"),
    [("shared_main", ["thread"]), ("private_main", ["thread", "far"])],
)
async def test_teleport_crosses_only_out_of_a_conversation_nobody_else_reads(
    in_memory_engine: None,
    shape: Shape,
    destinations: list[str],
) -> None:
    """Everything said here travels with a teleport. Out of a group that would
    republish what other people said into somewhere private on another platform,
    under this person's name alone — so the crossing is not offered at all, while
    the group's own sub-thread still is."""
    capability = await _crossable(shape, far_routes=(INKLING_ROUTE,))
    menu = await capability.session.operations
    assert menu.teleport.destinations == [
        capability.session.conversation_address
        if target == "thread"
        else ChannelAddress(
            channel_tentacle_id=target, chat_type="dm", chat_id="", user_id="ou_alice"
        )
        for target in destinations
    ]
    # Discovery is not a policy whitelist: a shared source may inspect the far
    # channel even though its history cannot teleport there.
    assert any(
        one
        == ChannelAddress(
            channel_tentacle_id="far", chat_type="dm", chat_id="", user_id="ou_alice"
        )
        for one in await capability.session.destinations()
    )


async def test_teleport_will_not_cross_to_a_channel_that_does_not_run_you(
    in_memory_engine: None,
) -> None:
    """A teleport takes *this* agent with it, so a channel that does not run this
    one has nowhere to put the conversation it carries."""
    capability = await _crossable("private_main", far_routes=(CLAUDE_ROUTE,))
    assert capability.toolset is not None
    teleport = capability.toolset.tools[GatewayTool.TELEPORT].function

    with pytest.raises(ModelRetry, match="does not run inkling"):
        await teleport(
            FAKE_CONTEXT,
            hint="carrying on",
            destination=ChannelAddress(
                channel_tentacle_id="far",
                chat_type="dm",
                chat_id="",
                user_id="ou_alice",
            ),
        )


async def test_teleport_defers_a_crossing_with_the_far_account_named(
    in_memory_engine: None,
) -> None:
    capability = await _crossable("private_main", far_routes=(INKLING_ROUTE,))
    assert capability.toolset is not None
    teleport = capability.toolset.tools[GatewayTool.TELEPORT].function

    with pytest.raises(CallDeferred) as deferred:
        await teleport(
            FAKE_CONTEXT,
            hint="carrying on",
            destination=ChannelAddress(
                channel_tentacle_id="far",
                chat_type="dm",
                chat_id="",
                user_id="ou_alice",
            ),
        )

    assert isinstance(capability.decision, TeleportDecision)
    assert capability.decision.destination is not None
    assert deferred.value.metadata == {
        "kind": "teleport",
        "hint": "carrying on",
        "destination": asdict(capability.decision.destination),
        "new_thread": True,
        "project": "",
        "ref": "",
        "resume": False,
    }


@pytest.mark.parametrize("parent", [None, "other-room"])
async def test_channel_target_does_not_require_discovery(
    in_memory_engine: None,
    monkeypatch: pytest.MonkeyPatch,
    parent: str | None,
) -> None:
    capability = await _crossable(
        "private_main", far_routes=(INKLING_ROUTE, CLAUDE_ROUTE)
    )
    session = capability.session
    ink = session.channels["far"].ink
    location = ChannelAddress(
        channel_tentacle_id="far",
        user_id="ou_alice",
        chat_type="group" if parent else "dm",
        chat_id=parent or "",
        shared=parent is not None,
    )
    lookup = AsyncMock(return_value=location)
    discover = AsyncMock(side_effect=AssertionError("Do not enumerate destinations"))
    monkeypatch.setattr(ink, "prepare_address", lookup)
    monkeypatch.setattr(session, "destinations", discover)
    target = location

    decision = await session.teleport(hint="Continue here", destination=target)
    assert decision.destination is not None
    crossing = decision.destination
    assert decision.agent_id == "inkling"

    assert crossing == ChannelAddress(
        channel_tentacle_id="far",
        chat_type=location.chat_type,
        chat_id=location.chat_id,
        user_id="ou_alice",
        shared=location.shared,
    )
    lookup.assert_awaited_once_with(target, None, private=True)
    discover.assert_not_awaited()
    assert session.destination_cache is None


@pytest.mark.parametrize(
    "blocked", ["shared", "unlinked", "unknown", "parent", "agent"]
)
async def test_explicit_teleport_parent_keeps_policy_checks(
    in_memory_engine: None, monkeypatch: pytest.MonkeyPatch, blocked: str
) -> None:
    capability = await _crossable(
        "shared_main" if blocked == "shared" else "private_main",
        far_routes=(CLAUDE_ROUTE,) if blocked == "agent" else (INKLING_ROUTE,),
    )
    session = capability.session
    if blocked == "unlinked":
        session.users = None
    lookup = AsyncMock(
        return_value=ChannelAddress(
            channel_tentacle_id="far",
            user_id="ou_alice",
            chat_type="group",
            chat_id="room",
            shared=True,
        )
    )
    if blocked == "parent":
        lookup.side_effect = ValueError("Parent is inaccessible")
    monkeypatch.setattr(session.channels["far"].ink, "prepare_address", lookup)

    with pytest.raises(
        GatewayRefusal,
        match={
            "shared": "Shared history",
            "unlinked": "Link your profile",
            "unknown": "No connected channel",
            "parent": "Parent is inaccessible",
            "agent": "does not run",
        }[blocked],
    ):
        await session.teleport(
            hint="Continue",
            destination=ChannelAddress(
                channel_tentacle_id="missing" if blocked == "unknown" else "far",
                chat_type="group",
                chat_id="room",
                user_id="ou_alice",
            ),
        )
    assert session.decision is None
    if blocked in {"shared", "unlinked", "unknown"}:
        lookup.assert_not_awaited()


def test_explicit_teleport_parent_must_not_be_empty() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ChannelAddress | None).validate_python(
            {"kind": "channel", "channel": "far", "parent": ""}
        )


async def test_explicit_parent_requires_a_channel_validator(
    in_memory_engine: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = await _crossable("private_main", far_routes=(INKLING_ROUTE,))
    monkeypatch.setattr(
        capability.session.channels["far"].ink,
        "prepare_address",
        AsyncMock(side_effect=ValueError("unsupported parent")),
    )
    with pytest.raises(GatewayRefusal, match="unsupported parent"):
        await capability.session.teleport(
            hint="Continue",
            destination=ChannelAddress(
                channel_tentacle_id="far",
                chat_type="group",
                chat_id="room",
                user_id="ou_alice",
            ),
        )
    assert capability.session.decision is None


def _destination_kinds(capability: GatewayCapability, tool_name: str) -> list[str]:
    """The `kind` each of a spell's `destination` variants declares, sorted.

    Read off the tool definition rather than the annotation, because the definition
    is what the provider is actually handed — and what must stay identical run to
    run for the prompt cache to hold."""
    assert capability.toolset is not None
    schema = capability.toolset.tools[tool_name].tool_def.parameters_json_schema
    assert isinstance(schema, dict)
    defs = schema["$defs"]
    assert isinstance(defs, dict)
    kinds: list[str] = []
    for name, definition in defs.items():
        if not name.endswith("Target") or not isinstance(definition, dict):
            continue
        if definition.get("type") != "object":
            continue
        properties = definition["properties"]
        assert isinstance(properties, dict)
        kinds.append(str(properties["kind"]["const"]))
    return sorted(kinds)


def _schemas(capability: GatewayCapability) -> dict[str, object]:
    """What actually reaches the provider: the cached tool definitions."""
    assert capability.toolset is not None
    return {
        name: tool.tool_def.parameters_json_schema
        for name, tool in capability.toolset.tools.items()
    }


def test_tool_schemas_do_not_vary_with_dm_availability() -> None:
    # Tool definitions are a provider prompt-cache breakpoint and sit at the front of
    # the cached prefix, so anything address-derived is refused in the tool body rather
    # than kept out of the schema. If this ever fails, a conversation that moves
    # busts its whole prefix — system prompt included.
    reachable = _schemas(_capability("shared_thread"))
    for reason in ("no_surface", "already_private", "no_user"):
        assert _schemas(_blocked(reason)) == reachable

    # The same for the walls the other two spells hit: a group main refuses
    # `summon here`, and a channel that opens no sub-thread refuses both it and
    # `teleport`. Neither may reach the schema either.
    assert _schemas(_capability("shared_main")) == reachable
    assert _schemas(_capability(channel=_NoSubThreadChannel())) == reachable


def test_tool_schemas_do_not_carry_the_live_routes() -> None:
    # The spells take their route as plain `str`, validated in the body against
    # `inspect`'s list. Rendering the live routes as a `Literal` instead would put
    # runtime state in the tool block — the same cache breakpoint as above — and
    # would drown the schema in KnownModelName's ~500 entries.
    summon = _schemas(_capability())[GatewayTool.SUMMON]
    assert isinstance(summon, dict)
    properties = summon["properties"]
    assert isinstance(properties, dict)
    for arg in ("agent_id", "model"):
        assert properties[arg]["type"] == "string"
        assert "enum" not in properties[arg]


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("no_surface", "no direct messages"),
        ("already_private", "already that user's direct messages"),
        ("no_user", "no single user"),
    ],
)
async def test_scheme_refuses_with_the_reason_it_cannot_land(
    reason: PrivateBlocker,
    expected: str,
) -> None:
    capability = _blocked(reason)
    assert capability.toolset is not None
    scheme = capability.toolset.tools[GatewayTool.SCHEME].function

    with pytest.raises(ModelRetry, match=expected):
        await scheme(
            FAKE_CONTEXT, hint="Picking this up with you.", brief="Do the thing."
        )


async def test_scheme_records_a_decision_that_names_no_agent() -> None:
    # Who receives it is the DM's business, resolved against that thread — the model
    # picks a place, never a person.
    capability = _capability()
    assert capability.toolset is not None
    scheme = capability.toolset.tools[GatewayTool.SCHEME].function

    result = await scheme(
        FAKE_CONTEXT,
        hint="Picking this up with you here.",
        brief="Finish the migration write-up for this user.",
    )

    assert result == "Taking this to your direct messages on im."
    assert capability.decision == SchemeDecision(
        hint="Picking this up with you here.",
        destination=ChannelAddress(
            channel_tentacle_id="im",
            chat_type="dm",
            chat_id="",
            user_id="alice",
        ),
        brief="Finish the migration write-up for this user.",
    )


def _registered_session() -> OctomateSession:
    session = OctomateSession(channel_routes={}, current_agent_id="inkling")
    session.conversation_id = uuid7()
    return session


def test_the_manager_finds_a_session_by_its_conversation_while_registered() -> None:
    manager = GatewayManager()
    session = _registered_session()
    assert session.conversation_id is not None

    manager.register(session)
    assert manager.get(session.conversation_id) is session
    manager.unregister(session)
    assert manager.get(session.conversation_id) is None


def test_a_session_without_a_conversation_cannot_be_registered() -> None:
    with pytest.raises(ValueError, match="conversation id"):
        GatewayManager().register(
            OctomateSession(channel_routes={}, current_agent_id="inkling")
        )


def test_a_second_turn_on_a_live_conversation_is_refused_not_queued() -> None:
    # Nothing serialises two turns of one conversation, so the registry does the
    # one thing it can: the first arrival holds the conversation, a second is
    # refused outright, and the slot frees only when the holder's turn ends.
    manager = GatewayManager()
    first = _registered_session()
    second = OctomateSession(channel_routes={}, current_agent_id="inkling")
    second.conversation_id = first.conversation_id
    assert first.conversation_id is not None

    manager.register(first)
    with pytest.raises(RuntimeError, match="already has a turn at the gateway"):
        manager.register(second)
    assert manager.get(first.conversation_id) is first
    # A refused session owns nothing, so its exit evicts nobody.
    manager.unregister(second)
    assert manager.get(first.conversation_id) is first

    manager.unregister(first)
    manager.register(second)
    assert manager.get(first.conversation_id) is second


async def test_driving_registers_the_session_for_exactly_its_span() -> None:
    manager = GatewayManager()
    session = _registered_session()
    assert session.conversation_id is not None

    async with manager.driving(session):
        assert manager.get(session.conversation_id) is session
    assert manager.get(session.conversation_id) is None


async def test_driving_tolerates_a_gateway_that_was_never_built() -> None:
    # A disabled connection builds no session, and a run with no thread has no
    # conversation id — neither registers, and neither breaks the span.
    manager = GatewayManager()
    async with manager.driving(None):
        assert manager.sessions == {}
    async with manager.driving(
        OctomateSession(channel_routes={}, current_agent_id="i")
    ):
        assert manager.sessions == {}


async def test_driving_without_gateway_spells_still_holds_the_conversation() -> None:
    manager = GatewayManager()
    session = _registered_session()
    assert session.conversation_id is not None
    async with manager.driving(None, conversation_id=session.conversation_id):
        assert manager.get(session.conversation_id) is None
        with pytest.raises(RuntimeError, match="already has a turn"):
            manager.register(session)
        with pytest.raises(RuntimeError, match="already has a turn"):
            async with manager.driving(None, conversation_id=session.conversation_id):
                pytest.fail("a second turn acquired the guard")
        assert session.conversation_id in manager.sessions
    assert manager.sessions == {}
    async with manager.driving(session):
        with pytest.raises(RuntimeError, match="already has a turn"):
            async with manager.driving(None, conversation_id=session.conversation_id):
                pytest.fail("a second turn acquired the guard")
        assert manager.get(session.conversation_id) is session
    assert manager.sessions == {}


async def test_driving_rejects_mismatched_session_identity() -> None:
    manager = GatewayManager()
    with pytest.raises(ValueError, match="another conversation"):
        async with manager.driving(_registered_session(), conversation_id=uuid7()):
            pytest.fail("a mismatched session acquired the guard")
    assert manager.sessions == {}


async def test_summon_refuses_a_brief_over_the_cap() -> None:
    """Refused, never trimmed, and before the spell runs: the cap is the tool's own
    argument schema, which the model sees and pydantic-ai validates by, so nothing
    is recorded until the brief fits. The session's signature holds the same line
    for a caller that is not a tool."""
    capability = _capability("shared_thread")
    assert capability.toolset is not None
    tool = capability.toolset.tools[GatewayTool.SUMMON]
    over = "Please investigate the failing test. " * 300

    assert (
        tool.function_schema.json_schema["properties"]["summon"]["maxLength"] == 8_000
    )
    with pytest.raises(ValidationError, match="at most 8000 characters"):
        tool.function_schema.validator.validate_python(
            {
                "agent_id": "claude",
                "model": "opus",
                "reason": "needs coding",
                "hint": "Working on it",
                "summon": over,
            }
        )
    with pytest.raises(ValidationError, match="at most 8000 characters"):
        await tool.function(
            FAKE_CONTEXT,
            agent_id="claude",
            model="opus",
            reason="needs coding",
            hint="Working on it",
            summon=over,
        )

    assert capability.decision is None


async def test_scheme_refuses_a_brief_over_the_cap() -> None:
    capability = _capability()
    assert capability.toolset is not None
    tool = capability.toolset.tools[GatewayTool.SCHEME]
    over = "Finish the migration write-up for this user. " * 300

    assert tool.function_schema.json_schema["properties"]["brief"]["maxLength"] == 8_000
    with pytest.raises(ValidationError, match="at most 8000 characters"):
        tool.function_schema.validator.validate_python(
            {"hint": "Picking this up with you here.", "brief": over}
        )
    with pytest.raises(ValidationError, match="at most 8000 characters"):
        await tool.function(
            FAKE_CONTEXT, hint="Picking this up with you here.", brief=over
        )

    assert capability.decision is None


def test_a_decision_carries_no_brief_over_the_cap() -> None:
    """The cap is the decision's own field, so one built anywhere else — a scheme
    re-cast as a summon — holds the same line the spells do."""
    fields = {
        "action": "summon",
        "agent_id": "claude",
        "model": "opus",
        "reason": "needs coding",
        "hint": "Working on it",
        "summon": "x" * 8_000,
    }

    assert len(SummonDecision.model_validate(fields).summon) == 8_000
    with pytest.raises(ValidationError, match="at most 8000 characters"):
        SummonDecision.model_validate({**fields, "summon": "x" * 8_001})


@pytest.mark.parametrize("offered", [False, True])
async def test_operation_menu_uses_cached_channel_discovery(
    monkeypatch: pytest.MonkeyPatch, offered: bool
) -> None:
    session = _capability("shared_main").session
    channel = session.channels["im"]
    discover = AsyncMock(wraps=channel.ink.suggest_addresses)
    if not offered:
        discover.return_value = []
    monkeypatch.setattr(channel.ink, "suggest_addresses", discover)

    expected = [
        ChannelAddress("im", "dm", "", "alice"),
        *([session.conversation_address] if offered else []),
    ]
    await session.operations
    assert await session.destinations() == expected
    discover.assert_awaited_once_with(
        ChannelAddress("im", "dm", "", "alice"), session.conversation_address
    )


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("supported", [False, True])
async def test_local_thread_validation_does_not_discover_destinations(
    monkeypatch: pytest.MonkeyPatch, shape: Shape, supported: bool
) -> None:
    session = _capability(
        shape, channel=FakeChannelTentacle() if supported else _NoSubThreadChannel()
    ).session
    discover = AsyncMock(side_effect=AssertionError("Do not enumerate destinations"))
    monkeypatch.setattr(session, "destinations", discover)

    async def invoke() -> None:
        await session.teleport(hint="Continue here", destination=None)

    if supported and shape in {"private_main", "shared_main"}:
        await invoke()
        assert session.decision is not None
    else:
        with pytest.raises(
            GatewayRefusal, match=r"cannot contain|no conversation can land"
        ):
            await invoke()
        assert session.decision is None
    discover.assert_not_awaited()
    assert session.destination_cache is None


async def test_move_refuses_an_address_for_another_user(
    in_memory_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = (
        await _crossable("private_main", far_routes=(INKLING_ROUTE, CLAUDE_ROUTE))
    ).session
    prepare = AsyncMock()
    monkeypatch.setattr(session.channels["far"], "prepare_address", prepare)
    destination = ChannelAddress("far", "dm", "", "somebody-else")
    with pytest.raises(GatewayRefusal, match="requesting user"):
        await session.teleport(hint="Continue", destination=destination)
    prepare.assert_not_awaited()
    assert session.decision is None


@pytest.mark.parametrize(
    ("shape", "private"), [("private_main", True), ("shared_main", False)]
)
async def test_a_private_conversation_asks_for_a_private_landing(
    in_memory_engine: None, monkeypatch: pytest.MonkeyPatch, shape: Shape, private: bool
) -> None:
    """Moving a conversation should not widen who reads it: the channel is asked to
    list and open its landing as private where it can, and says what it got."""
    session = (await _crossable(shape)).session
    far = session.channels["far"]
    destination = ChannelAddress("far", "dm", "", "ou_alice")
    prepare = AsyncMock(return_value=destination)
    listing = AsyncMock(return_value=[])
    monkeypatch.setattr(far, "prepare_address", prepare)
    monkeypatch.setattr(far, "list_addresses", listing)

    await session.prepare_address(destination)
    await session.inspect("destinations", "far")

    assert session.lands_privately is private
    assert prepare.await_args is not None
    assert prepare.await_args.kwargs == {"private": private}
    assert listing.await_args is not None
    assert listing.await_args.kwargs == {"private": private}


async def test_inspect_routes_for_an_address_does_not_discover_destinations(
    in_memory_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = await _crossable(
        "private_main", far_routes=(CLAUDE_ROUTE, INKLING_ROUTE)
    )
    discover = AsyncMock(side_effect=AssertionError("Do not enumerate destinations"))
    monkeypatch.setattr(capability.session, "destinations", discover)
    assert await capability.inspect(FAKE_CONTEXT, "routes", channel="far") == [
        CLAUDE_ROUTE
    ]
    capability.session.users = None
    with pytest.raises(ModelRetry, match="Link your profile"):
        await capability.inspect(FAKE_CONTEXT, "routes", channel="far")
    discover.assert_not_awaited()


async def test_inspect_browses_a_channel_one_level_at_a_time(
    in_memory_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = await _crossable(
        "private_main", far_routes=(CLAUDE_ROUTE, INKLING_ROUTE)
    )
    server = ChannelAddress(
        "far",
        "group",
        "",
        "ou_alice",
        shared=True,
        metadata={"name": "Community", "inside": "200"},
    )
    room = ChannelAddress("far", "group", "400", "ou_alice", shared=True)
    listing = AsyncMock(side_effect=[[server], [room]])
    monkeypatch.setattr(
        capability.session.channels["far"].ink, "list_addresses", listing
    )
    requester = ChannelAddress("far", "dm", "", "ou_alice")

    assert await capability.inspect(FAKE_CONTEXT, "destinations", channel="far") == [
        server
    ]
    assert await capability.inspect(
        FAKE_CONTEXT, "destinations", channel="far", inside="200"
    ) == [room]
    assert [call.args for call in listing.await_args_list] == [
        (requester, None),
        (requester, "200"),
    ]
    # `inside` names a place a channel listed, so it needs the channel.
    with pytest.raises(ModelRetry, match="`inside` opens a place"):
        await capability.inspect(FAKE_CONTEXT, "destinations", inside="200")
    with pytest.raises(ModelRetry, match="Only routes and destinations"):
        await capability.inspect(FAKE_CONTEXT, "projects", channel="far")
    with pytest.raises(ModelRetry, match="cannot be browsed"):
        await capability.inspect(FAKE_CONTEXT, "destinations", channel="im")


async def test_a_native_call_binds_only_a_session_its_caller_wrote(
    in_memory_engine: None,
) -> None:
    """A native call says which session it came from; the session binds to that
    session's thread only when the caller has written to it, so naming someone
    else's session, or one never uploaded, finds nothing to move."""
    owner = await a_user("luhui")
    users = UserManager()
    threads = ThreadManager(users=users)
    profile = await users.native_profile(CLAUDE_NATIVE_ID, "luhui")
    assert profile is not None
    written = await threads.ensure(ThreadKey(CLAUDE_NATIVE_ID, "thread", "sess-1"))
    await threads.ensure(ThreadKey(CLAUDE_NATIVE_ID, "thread", "sess-2"))
    await threads.record_outbound(
        written,
        agent_tentacle_id=CLAUDE_NATIVE_ID,
        segments=[TextSegment(data={"text": "native history"})],
        sender=UserProfile(
            channel_user_id=owner.username, user_id=owner.id, name=owner.name
        ),
    )
    session = OctomateSession(
        channel_routes={},
        current_agent_id=CLAUDE_NATIVE_ID,
        users=users,
        user_profile=profile,
        native=True,
        threads=threads,
    )

    for elsewhere in ("sess-2", "sess-unknown"):
        with pytest.raises(GatewayRefusal, match="no uploaded history"):
            await session.attach_native_thread(elsewhere)
    assert session.thread_id is None
    await session.attach_native_thread("sess-1")

    assert session.thread_id == written.id
    assert session.conversation_address == ChannelAddress(
        CLAUDE_NATIVE_ID, "thread", "sess-1", profile.channel_user_id
    )
    assert session.teleport_unavailable is None
