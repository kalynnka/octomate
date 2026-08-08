from __future__ import annotations

from typing import ClassVar, cast

import pytest
from pydantic import ValidationError
from pydantic_ai import CallDeferred, RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.settings import ThinkingEffort

from octomate.capabilities.gateway import (
    SCHEME_TOOL_NAME,
    SCRY_TOOL_NAME,
    SUMMON_TOOL_NAME,
    TELEPORT_TOOL_NAME,
    GatewayCapability,
    PrivateBlocker,
)
from octomate.config.agents import AgentRouteModelName
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import (
    AgentRoute,
    Claim,
    SchemeDecision,
    SummonDecision,
    SummonDestination,
)
from octomate.tentacles.channels.base import ChannelSurfaces
from tests.support.channels import FakeChannelTentacle

FAKE_CONTEXT = cast(RunContext[None], None)


CLAUDE_CLAIM = Claim(ability="coding work", efforts=("medium", "high"))


class _NoDmChannel(FakeChannelTentacle):
    surfaces: ClassVar[ChannelSurfaces] = ChannelSurfaces(sub_thread=True)


def _capability(
    allow_here: bool = True,
    private_blocked_by: PrivateBlocker | None = None,
) -> GatewayCapability:
    """A gate that allows `summon here` for `allow_here`, and whose `scheme` is
    refused for `private_blocked_by` or reachable for None.

    Both are derived from the channel's surfaces and the run's own address, so ask
    for the pair you want and get the channel and address producing it — the closing
    asserts keep the two ends honest. `test_reflex_graph` covers the derivations."""
    capability = GatewayCapability(
        routes=[
            AgentRoute(
                agent_id="claude",
                model="opus",
                claim=CLAUDE_CLAIM,
            ),
            AgentRoute(
                agent_id="inkling",
                model="deepseek:deepseek-chat",
                claim=Claim(
                    ability="current agent",
                    efforts=("low", "medium", "high"),
                ),
            ),
        ],
        current_agent_id="inkling",
        channels={
            "im": (
                _NoDmChannel()
                if private_blocked_by == "no_surface"
                else FakeChannelTentacle()
            )
        },
        conversation_address=ChannelAddress(
            channel_tentacle_id="im",
            # A group's main channel is the one place `summon here` is refused.
            chat_type=(
                "dm"
                if private_blocked_by == "already_private"
                else "thread"
                if allow_here
                else "group"
            ),
            chat_id="room",
            channel_thread_id="t-1"
            if allow_here and private_blocked_by is None
            else "",
            user_id="" if private_blocked_by == "no_user" else "alice",
        ),
    )
    assert capability.allow_here == allow_here
    assert capability.private_blocked_by == private_blocked_by
    return capability


def _decision(
    agent_id: str = "claude",
    model: AgentRouteModelName = "opus",
    destination: SummonDestination = "thread",
    effort: ThinkingEffort | None = None,
) -> SummonDecision:
    return SummonDecision(
        action="summon",
        agent_id=agent_id,
        model=model,
        destination=destination,
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
    assert _decision().destination == "thread"


async def test_summon_capability_accepts_exact_route() -> None:
    capability = _capability()
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    await summon(
        FAKE_CONTEXT,
        agent_id="claude",
        model="opus",
        destination="thread",
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
    capability = _capability()
    assert capability.toolset is not None
    scry = capability.toolset.tools[SCRY_TOOL_NAME].function

    scrying = await scry(FAKE_CONTEXT)

    assert scrying.routes == [
        AgentRoute(
            agent_id="claude",
            model="opus",
            claim=CLAUDE_CLAIM,
        )
    ]
    # The same list every spell resolves against — built-ins, then anywhere the
    # asker is registered. This run is a group thread, so all three are offered.
    assert [one.handle for one in scrying.destinations] == ["here", "dm", "thread"]


async def test_summon_capability_rejects_self_summon() -> None:
    capability = _capability()
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    with pytest.raises(ModelRetry, match="Cannot summon yourself"):
        await summon(
            FAKE_CONTEXT,
            agent_id="inkling",
            model="opus",
            destination="thread",
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
            destination="thread",
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
        destination="thread",
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
            destination="thread",
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
            effort="low",
        )
    assert capability.decision is None


async def test_summon_here_refused_when_disallowed() -> None:
    capability = _capability(allow_here=False)
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    with pytest.raises(ModelRetry, match="group's main channel"):
        await summon(
            FAKE_CONTEXT,
            agent_id="claude",
            model="opus",
            destination="here",
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )
    assert capability.decision is None


async def test_summon_here_allowed_on_bounded_surface() -> None:
    capability = _capability(allow_here=True)
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    await summon(
        FAKE_CONTEXT,
        agent_id="claude",
        model="opus",
        destination="here",
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )

    assert capability.decision == _decision(destination="here")


async def test_summon_tool_records_decision() -> None:
    capability = _capability()
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    result = await summon(
        FAKE_CONTEXT,
        agent_id="claude",
        model="opus",
        destination="thread",
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
    reachable = _schemas(_capability(private_blocked_by=None))
    for reason in ("no_surface", "already_private", "no_user"):
        assert _schemas(_capability(private_blocked_by=reason)) == reachable

    assert _schemas(_capability(allow_here=False)) == reachable


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
    capability = _capability(private_blocked_by=reason)
    assert capability.toolset is not None
    scheme = capability.toolset.tools[SCHEME_TOOL_NAME].function

    with pytest.raises(ModelRetry, match=expected):
        await scheme(FAKE_CONTEXT, hint="Taking this private", brief="Do the thing.")


async def test_scheme_records_a_decision_that_names_no_agent() -> None:
    # Who receives it is the DM's business, resolved against that thread — the model
    # picks a place, never a person.
    capability = _capability()
    assert capability.toolset is not None
    scheme = capability.toolset.tools[SCHEME_TOOL_NAME].function

    result = await scheme(
        FAKE_CONTEXT,
        hint="Continuing with you privately",
        brief="Finish the migration write-up for this user.",
    )

    assert result == "Taking this to their direct messages here."
    assert capability.decision == SchemeDecision(
        destination=ChannelAddress(
            channel_tentacle_id="im",
            chat_type="dm",
            chat_id="",
            user_id="alice",
        ),
        hint="Continuing with you privately",
        brief="Finish the migration write-up for this user.",
    )
