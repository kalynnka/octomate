from __future__ import annotations

from typing import ClassVar, Literal, cast

import pytest
from pydantic import ValidationError
from pydantic_ai import CallDeferred, RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.settings import ThinkingEffort
from uuid_utils.compat import uuid7

from octomate.capabilities.gateway import GatewayCapability
from octomate.config import AgentModelConfig, ChannelConfig
from octomate.config.agents import AgentRouteModelName
from octomate.config.users import UserConfig
from octomate.managers.gateway import GatewayManager, GatewaySession, PrivateBlocker
from octomate.managers.user import UserManager
from octomate.schemas.conversation import ChannelAddress, ChatType
from octomate.schemas.messages import SEND_TOOL_NAME
from octomate.schemas.triage import (
    HERE_TARGET,
    SCHEME_TOOL_NAME,
    SCRY_TOOL_NAME,
    SUMMON_TOOL_NAME,
    TELEPORT_TOOL_NAME,
    THREAD_TARGET,
    AgentRoute,
    ChannelTarget,
    Claim,
    CrossingLanding,
    HereLanding,
    SchemeDecision,
    SummonDecision,
    SummonLanding,
    SummonTarget,
    ThreadLanding,
)
from octomate.schemas.user import UserProfile
from octomate.tentacles.channel import ChannelSurfaces
from tests.support.agents import FakeAgent
from tests.support.channels import FakeChannelTentacle

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
# the surface is private — which is why the summon tests below sit on a group main.
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
        session=GatewaySession(
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
    destination: SummonLanding | None = None,
    effort: ThinkingEffort | None = None,
) -> SummonDecision:
    return SummonDecision(
        action="summon",
        agent_id=agent_id,
        model=model,
        destination=destination or ThreadLanding(),
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


@pytest.mark.parametrize("model", [None, ""])
def test_summon_decision_requires_concrete_model(model: str | None) -> None:
    with pytest.raises(ValidationError, match="model"):
        SummonDecision(
            action="summon",
            agent_id="claude",
            # Same: `None` and `""` are the rejected values this parametrises over.
            model=model,  # pyright: ignore[reportArgumentType]
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )


def test_summon_decision_defaults_to_thread_destination() -> None:
    assert (
        SummonDecision(
            action="summon",
            agent_id="claude",
            model="opus",
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        ).destination
        == ThreadLanding()
    )


async def test_summon_capability_accepts_exact_route() -> None:
    capability = _capability()
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    await summon(
        FAKE_CONTEXT,
        agent_id="claude",
        model="opus",
        destination=THREAD_TARGET,
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )

    assert capability.decision == _decision()


def test_gate_instruction_explains_each_spell_in_plain_words() -> None:
    instructions = _capability().get_instructions()

    assert "`scry`" in instructions
    assert "`summon`" in instructions
    assert "`teleport`" in instructions
    assert "route the conversation" in instructions
    # No concrete route details leak into the shared instruction block.
    assert "agent_id=claude" not in instructions
    assert "coding work" not in instructions
    assert "target_id" not in instructions


async def test_scry_tool_returns_other_routes() -> None:
    # A shared thread: the one shape that offers both built-ins at once.
    capability = _capability("shared_thread")
    assert capability.toolset is not None
    scry = capability.toolset.tools[SCRY_TOOL_NAME].function

    routes = await scry(FAKE_CONTEXT, "routes")
    places = await scry(FAKE_CONTEXT, "destinations")

    assert routes == [
        AgentRoute(
            agent_id="claude",
            model="opus",
            claim=CLAUDE_CLAIM,
        )
    ]
    # The same list every spell resolves against — built-ins, then anywhere the
    # asker is registered. A sub-thread is not among them: `summon` names one
    # through its own literal, and a resolved handle is always somewhere a person
    # can be delivered to.
    assert [one.handle for one in places] == ["here", "dm"]


async def test_scry_computes_only_the_facet_it_was_asked_for() -> None:
    capability = _capability("shared_thread")
    assert capability.toolset is not None
    scry = capability.toolset.tools[SCRY_TOOL_NAME].function

    await scry(FAKE_CONTEXT, "routes")

    # The registry was never reached for the facet nobody asked for.
    assert capability.session.computed_destinations is None


async def test_summon_capability_rejects_self_summon() -> None:
    capability = _capability()
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    with pytest.raises(ModelRetry, match="Cannot summon yourself"):
        await summon(
            FAKE_CONTEXT,
            agent_id="inkling",
            model="opus",
            destination=THREAD_TARGET,
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
    capability = _capability()
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    with pytest.raises(ModelRetry, match="Invalid summon route"):
        await summon(
            FAKE_CONTEXT,
            agent_id=agent_id,
            model=model,
            destination=THREAD_TARGET,
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )


async def test_summon_carries_a_claimed_effort() -> None:
    capability = _capability()
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    await summon(
        FAKE_CONTEXT,
        agent_id="claude",
        model="opus",
        destination=THREAD_TARGET,
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
        effort="high",
    )

    assert capability.decision == _decision(effort="high")


async def test_summon_refuses_an_unclaimed_effort() -> None:
    capability = _capability()
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    with pytest.raises(ModelRetry, match="does not accept effort 'low'"):
        await summon(
            FAKE_CONTEXT,
            agent_id="claude",
            model="opus",
            destination=THREAD_TARGET,
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
        session=GatewaySession(
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
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    with pytest.raises(ModelRetry, match="group's main channel"):
        await summon(
            FAKE_CONTEXT,
            agent_id="claude",
            model="opus",
            destination=HERE_TARGET,
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )
    assert capability.decision is None


async def test_summon_here_allowed_on_bounded_surface() -> None:
    capability = _capability("shared_thread")
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    await summon(
        FAKE_CONTEXT,
        agent_id="claude",
        model="opus",
        destination=HERE_TARGET,
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )

    assert capability.decision == _decision(destination=HereLanding())


async def test_summon_tool_records_decision() -> None:
    capability = _capability()
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    result = await summon(
        FAKE_CONTEXT,
        agent_id="claude",
        model="opus",
        destination=THREAD_TARGET,
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )

    assert result == "Summoning claude (opus) → thread."
    assert capability.decision == _decision()


async def test_teleport_defers_the_run() -> None:
    capability = _capability()
    assert capability.toolset is not None
    teleport = capability.toolset.tools[TELEPORT_TOOL_NAME].function

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
    teleport = capability.toolset.tools[TELEPORT_TOOL_NAME].function

    with pytest.raises(ModelRetry, match="nowhere left to land"):
        await teleport(FAKE_CONTEXT, hint="let's move to a thread")


def test_each_spell_declares_only_the_places_it_goes() -> None:
    """The schema is the refusal for a place a spell never goes, so the body never
    has to be. `here` is where a summon hands over and a send delivers, and where
    a teleport stays put only to bind a project — the body refuses it without one;
    `dm` is what a scheme means, and a summon into someone's direct messages is a
    scheme by another name."""
    capability = _capability()
    assert capability.toolset is not None

    assert _destination_kinds(capability, SUMMON_TOOL_NAME) == [
        "channel",
        "here",
        "thread",
    ]
    assert _destination_kinds(capability, TELEPORT_TOOL_NAME) == [
        "channel",
        "here",
        "thread",
    ]
    assert _destination_kinds(capability, SCHEME_TOOL_NAME) == ["channel", "dm"]
    assert _destination_kinds(capability, SEND_TOOL_NAME) == ["channel", "dm", "here"]


@pytest.mark.parametrize(
    ("shape", "channel"),
    [
        # Already in one — nothing nests.
        ("shared_thread", None),
        # A main, but on a channel that opens none at all.
        ("private_main", _NoSubThreadChannel()),
    ],
)
async def test_summon_thread_refused_where_no_sub_thread_can_be_opened(
    shape: Shape,
    channel: FakeChannelTentacle | None,
) -> None:
    capability = _capability(shape, channel=channel)
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    with pytest.raises(ModelRetry, match="No sub-thread to open here"):
        await summon(
            FAKE_CONTEXT,
            agent_id="claude",
            model="opus",
            destination=THREAD_TARGET,
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )
    assert capability.decision is None


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
                agents=[
                    AgentModelConfig(agent=route.agent_id, model=route.model)
                    for route in far_routes
                ],
            ),
        ),
    }
    session.agents = {
        route.agent_id: FakeAgent(id=route.agent_id) for route in far_routes
    }
    return capability


async def test_summon_crosses_to_a_sub_thread_of_their_dms_elsewhere(
    in_memory_engine: None,
) -> None:
    """A group's main channel refuses a handoff in place and opens no sub-thread of
    its own — but the asker is on another channel that does. The landing names that
    channel and the account on it; the chat id is still empty, because which
    conversation it is only exists once the channel opens it."""
    capability = await _crossable()
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    await summon(
        FAKE_CONTEXT,
        agent_id="claude",
        model="opus",
        destination=ChannelTarget(channel="far"),
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )

    assert isinstance(capability.decision, SummonDecision)
    landing = capability.decision.destination
    assert isinstance(landing, CrossingLanding)
    assert landing.address.channel_tentacle_id == "far"
    assert landing.address.user_id == "ou_alice"
    assert landing.address.chat_id == ""


async def test_summon_will_not_cross_to_a_channel_that_opens_no_sub_thread(
    in_memory_engine: None,
) -> None:
    """`scheme` reaches a channel like this — it lands in the direct messages
    themselves. A summon lands in a sub-thread of them, so there is nowhere for it
    to go and the channel is not offered at all."""
    capability = await _crossable(
        far_channel=_NoSubThreadChannel(
            id="far",
            config=ChannelConfig(
                type="fake", agents=[AgentModelConfig(agent="claude", model="opus")]
            ),
        )
    )
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    assert await capability.session.crossing_destinations() == []
    with pytest.raises(ModelRetry, match="No destination 'far'"):
        await summon(
            FAKE_CONTEXT,
            agent_id="claude",
            model="opus",
            destination=ChannelTarget(channel="far"),
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )
    assert capability.decision is None


async def test_summon_across_names_the_agents_the_far_channel_runs(
    in_memory_engine: None,
) -> None:
    """Which agents serve a channel is that channel's own config, so crossing both
    widens what can be summoned and narrows it: an agent that only runs over there
    becomes nameable, and one that only runs here stops being."""
    only_far = AgentRoute(
        agent_id="codex", model="opus", claim=Claim(ability="far-side coding")
    )
    capability = await _crossable(far_routes=(only_far,))
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    # `codex` is on no route here, and `scry` says where it is instead.
    assert only_far not in capability.session.other_routes
    assert [one.routes for one in await capability.session.crossing_destinations()] == [
        (only_far,)
    ]

    await summon(
        FAKE_CONTEXT,
        agent_id="codex",
        model="opus",
        destination=ChannelTarget(channel="far"),
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )
    assert isinstance(capability.decision, SummonDecision)
    assert capability.decision.agent_id == "codex"

    # And the other way: `claude` runs here, but not there.
    with pytest.raises(ModelRetry, match="Invalid summon route"):
        await summon(
            FAKE_CONTEXT,
            agent_id="claude",
            model="opus",
            destination=ChannelTarget(channel="far"),
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )


async def test_teleport_crosses_only_out_of_a_conversation_nobody_else_reads(
    in_memory_engine: None,
) -> None:
    """Everything said here travels with a teleport. Out of a group that would
    republish what other people said into somewhere private on another platform,
    under this person's name alone — so the crossing is not offered at all, while
    the group's own sub-thread still is."""
    shared = await _crossable("shared_main", far_routes=(INKLING_ROUTE,))
    assert await shared.session.teleport_handles() == ["thread"]

    private = await _crossable("private_main", far_routes=(INKLING_ROUTE,))
    assert await private.session.teleport_handles() == ["thread", "far"]


async def test_teleport_will_not_cross_to_a_channel_that_does_not_run_you(
    in_memory_engine: None,
) -> None:
    """A teleport takes *this* agent with it, so a channel that does not run this
    one has nowhere to put the conversation it carries."""
    capability = await _crossable("private_main", far_routes=(CLAUDE_ROUTE,))
    assert capability.toolset is not None
    teleport = capability.toolset.tools[TELEPORT_TOOL_NAME].function

    with pytest.raises(ModelRetry, match="does not run you \\(inkling\\)"):
        await teleport(
            FAKE_CONTEXT, hint="carrying on", destination=ChannelTarget(channel="far")
        )


async def test_teleport_defers_a_crossing_with_the_far_account_named(
    in_memory_engine: None,
) -> None:
    capability = await _crossable("private_main", far_routes=(INKLING_ROUTE,))
    assert capability.toolset is not None
    teleport = capability.toolset.tools[TELEPORT_TOOL_NAME].function

    with pytest.raises(CallDeferred) as deferred:
        await teleport(
            FAKE_CONTEXT, hint="carrying on", destination=ChannelTarget(channel="far")
        )

    # Two plain strings, which is all the far end needs: `open_dm` takes the account,
    # and the conversation it names does not exist until that call.
    assert deferred.value.metadata == {
        "kind": "teleport",
        "hint": "carrying on",
        "channel": "far",
        "user": "ou_alice",
        "here": False,
        "project": "",
        "ref": "",
    }


@pytest.mark.parametrize("destination", [HERE_TARGET, THREAD_TARGET])
async def test_summon_refused_outright_where_neither_place_exists(
    destination: SummonTarget,
) -> None:
    """A group's main channel on a channel that opens no sub-thread — napcat's
    groups, with nobody linked anywhere else. Ownership cannot land in place, there
    is nowhere to open, and no channel to cross to, so the refusal has no "instead"
    to name and says to answer it here."""
    capability = _capability("shared_main", channel=_NoSubThreadChannel())
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    with pytest.raises(ModelRetry, match="nowhere left to land"):
        await summon(
            FAKE_CONTEXT,
            agent_id="claude",
            model="opus",
            destination=destination,
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )
    assert capability.decision is None


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
    # `scry`'s list. Rendering the live routes as a `Literal` instead would put
    # runtime state in the tool block — the same cache breakpoint as above — and
    # would drown the schema in KnownModelName's ~500 entries.
    summon = _schemas(_capability())[SUMMON_TOOL_NAME]
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
    scheme = capability.toolset.tools[SCHEME_TOOL_NAME].function

    with pytest.raises(ModelRetry, match=expected):
        await scheme(
            FAKE_CONTEXT, hint="Picking this up with you.", brief="Do the thing."
        )


async def test_scheme_records_a_decision_that_names_no_agent() -> None:
    # Who receives it is the DM's business, resolved against that thread — the model
    # picks a place, never a person.
    capability = _capability()
    assert capability.toolset is not None
    scheme = capability.toolset.tools[SCHEME_TOOL_NAME].function

    result = await scheme(
        FAKE_CONTEXT,
        hint="Picking this up with you here.",
        brief="Finish the migration write-up for this user.",
    )

    assert result == "Taking this to their direct messages here."
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


def _registered_session() -> GatewaySession:
    session = GatewaySession(channel_routes={}, current_agent_id="inkling")
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
            GatewaySession(channel_routes={}, current_agent_id="inkling")
        )


def test_a_second_turn_on_a_live_conversation_is_refused_not_queued() -> None:
    # Nothing serialises two turns of one conversation, so the registry does the
    # one thing it can: the first arrival holds the conversation, a second is
    # refused outright, and the slot frees only when the holder's turn ends.
    manager = GatewayManager()
    first = _registered_session()
    second = GatewaySession(channel_routes={}, current_agent_id="inkling")
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


def test_driving_registers_the_session_for_exactly_its_span() -> None:
    manager = GatewayManager()
    session = _registered_session()
    assert session.conversation_id is not None

    with manager.driving(session):
        assert manager.get(session.conversation_id) is session
    assert manager.get(session.conversation_id) is None


def test_driving_tolerates_a_gateway_that_was_never_built() -> None:
    # A disabled connection builds no session, and a run with no thread has no
    # conversation id — neither registers, and neither breaks the span.
    manager = GatewayManager()
    with manager.driving(None):
        assert manager.sessions == {}
    with manager.driving(GatewaySession(channel_routes={}, current_agent_id="i")):
        assert manager.sessions == {}
