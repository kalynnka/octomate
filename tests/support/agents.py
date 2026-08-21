"""The canonical fake agents.

`FakeAgent` stands in for an `AgentTentacle` at the tentacle level: it records
run/stream calls and plays scripted outputs (a channel answer, deferred
requests, or a scenarios event script). The `Scripted*` builders operate at the
model level instead — they build real pydantic-ai agents around a scripted
`FunctionModel`, for tests that drive the actual react graph.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Sequence,
)
from dataclasses import dataclass, field
from typing import ClassVar, cast

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage
from claude_agent_sdk.types import Message as ClaudeMessage
from pydantic_ai import (
    AgentCapability,
    AgentRunResult,
    AgentRunResultEvent,
    RunContext,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserContent,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.output import OutputSpec
from pydantic_ai.settings import ThinkingEffort
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from octomate import Octomate
from octomate.capabilities.ask import AskCapability
from octomate.capabilities.gateway import (
    GatewayCapability,
)
from octomate.capabilities.harness.agent import Agent
from octomate.capabilities.harness.deferred import DeferredSuspender
from octomate.capabilities.harness.react import ReactEventStream, ReactStreamEvent
from octomate.config.agents import (
    AgentRouteModelName,
    ClaudeCodeModelName,
    CodexModelName,
    DeepseekModelName,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import (
    DIRECT_TARGET,
    HERE_TARGET,
    SCHEME_TOOL_NAME,
    SUMMON_TOOL_NAME,
    TELEPORT_DEFER_KIND,
    TELEPORT_TOOL_NAME,
    THREAD_TARGET,
    ChannelTarget,
    Claim,
    CrossingLanding,
    HereLanding,
    SchemeDecision,
    SchemeTarget,
    SummonDecision,
    SummonTarget,
)
from octomate.tentacles.agents.base import AgentTentacle
from octomate.tentacles.agents.inkling.prompts import SYSTEM_PROMPT
from octomate.tentacles.channels.base import ChannelOutput
from octomate.types.json import JsonObject
from tests.support.scenarios import plain_answer, play

FakeRunOutput = ChannelOutput
ScriptedOutput = str | DeferredToolRequests

# Every agent config requires at least one model — nothing is defaulted, since which
# LLM an operator holds keys for is not guessable. These are what a test names when
# the model set is not what it is testing.
CLAUDE_MODELS: frozenset[ClaudeCodeModelName] = frozenset(
    {"opusplan[1m]", "opus[1m]", "sonnet[1m]", "haiku"}
)
CODEX_MODELS: frozenset[CodexModelName] = frozenset(
    {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.5-pro",
        "gpt-5.3-codex",
        "gpt-5.1-codex-mini",
    }
)
DEEPSEEK_MODELS: frozenset[DeepseekModelName] = frozenset(
    {"deepseek-v4-flash", "deepseek-v4-pro"}
)


def _teleport_requests(hint: str, destination: str = "thread") -> DeferredToolRequests:
    """A reception run's `teleport` deferral — the suspender skips it and the graph
    forks + resumes. On the resumed run (deferred results present) the fake answers
    normally, so a teleport turn does not loop.

    `destination` is the handle a model would name, and the metadata is what the gate
    puts there once it has resolved one: a crossing carries the far channel and the
    account on it, and `thread` carries neither."""
    crossing = destination != "thread"
    return DeferredToolRequests(
        calls=[
            ToolCallPart(
                tool_name=TELEPORT_TOOL_NAME,
                args={"hint": hint, "destination": destination},
                tool_call_id="call_teleport",
            )
        ],
        metadata={
            "call_teleport": {
                "kind": TELEPORT_DEFER_KIND,
                "hint": hint,
                "channel": destination if crossing else "",
                "user": "ou_alice" if crossing else "",
            }
        },
    )


def _gate_tool(
    capabilities: Sequence[AgentCapability[None]] | None,
    tool_name: str,
) -> Callable[..., Awaitable[str]]:
    gate = next(
        (
            capability
            for capability in capabilities or []
            if isinstance(capability, GatewayCapability)
        ),
        None,
    )
    if gate is None or gate.toolset is None:
        raise AssertionError("a gate decision requires a mounted gate toolset")
    return gate.toolset.tools[tool_name].function


def scheme_target(
    decision: SchemeDecision, capabilities: Sequence[AgentCapability[None]] | None
) -> SchemeTarget:
    """What a model would have named to produce `decision`'s destination.

    Their direct messages on the channel the run is already on, or the channel it
    names for anywhere else — the same conversion the gate runs forwards.
    """
    gate = next(
        capability
        for capability in capabilities or []
        if isinstance(capability, GatewayCapability)
    )
    here = gate.session.conversation_address
    far = decision.destination.channel_tentacle_id
    if here is not None and far == here.channel_tentacle_id:
        return DIRECT_TARGET
    return ChannelTarget(channel=far)


def summon_target(decision: SummonDecision) -> SummonTarget:
    """What a model would have named to produce `decision`'s landing.

    A fake configured with a decision still calls the *real* summon tool, which
    takes the target and resolves the landing itself — so a replay has to run the
    conversion backwards.
    """
    landing = decision.destination
    if isinstance(landing, CrossingLanding):
        return ChannelTarget(channel=landing.address.channel_tentacle_id)
    if isinstance(landing, HereLanding):
        return HERE_TARGET
    return THREAD_TARGET


@dataclass
class RecordedRun:
    prompt: str | Sequence[UserContent] | None
    history: list[ModelMessage]
    address: ChannelAddress
    run_name: str | None
    thread_id: uuid.UUID | None = None
    source_thread_address: ChannelAddress | None = None
    source_thread_message_ids: list[uuid.UUID] = field(default_factory=list)
    deferred_results: DeferredToolResults | None = None
    model: Model | str | None = None
    effort: ThinkingEffort | None = None
    conversation_id: uuid.UUID | None = None
    interactive: bool = True
    instructions: str | None = None
    capabilities: list[AgentCapability[None]] = field(default_factory=list)


@dataclass
class FakeAgent(AgentTentacle[FakeRunOutput, None]):
    """Stands in for an AgentTentacle. Because the react graph is short-circuited
    here, the fake itself honors the AgentTentacle contract: when its output is a
    DeferredToolRequests it invokes the supplied suspender, exactly as react would.
    """

    id: str = "inkling"
    description: str = "fake agent"
    octomate: Octomate | None = None
    reception_output: ChannelOutput = "handled"
    reception_summon: SummonDecision | None = None
    reception_scheme: SchemeDecision | None = None
    # When set, the first reception run emits a `teleport` deferral with this hint;
    # the resumed run (deferred results present) falls through to `reception_output`.
    reception_teleport: str | None = None
    reception_teleport_destination: str = "thread"
    # When set, the first reception run records a teleport decision directly on the
    # mounted gateway's session — the way a non-deferring external runtime's MCP tool
    # does — and ends its turn normally; the resumed run gets `reception_output`.
    reception_recorded_teleport: str | None = None
    reception_script: list[ReactStreamEvent[ChannelOutput]] | None = None
    allow_reception_run: bool = False
    models: dict[AgentRouteModelName, Model | str] = field(
        default_factory=lambda: {
            "test": "fake-model",
            "deepseek:deepseek-v4-flash": "fake-model",
            "deepseek:deepseek-v4-pro": "fake-model",
            "opus": "fake-model",
            "opusplan[1m]": "fake-model",
        }
    )
    # The agent's side of the gateway switch, as a real tentacle reads it off its
    # config block.
    gateway: bool = True
    # An unclaimed model is not routable, so the fake claims every model it
    # ships by default; pass claims={} to fake an agent that advertises nothing.
    claims: dict[AgentRouteModelName, Claim] = field(
        default_factory=lambda: {
            model: Claim(ability="fake agent")
            for model in (
                "test",
                "deepseek:deepseek-v4-flash",
                "deepseek:deepseek-v4-pro",
                "opus",
                "opusplan[1m]",
            )
        }
    )
    turns: list[RecordedRun] = field(default_factory=list)
    streams: list[RecordedRun] = field(default_factory=list)

    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[FakeRunOutput] | None = None,
        model: Model | str | None = None,
        effort: ThinkingEffort | None = None,
        conversation_id: uuid.UUID | None = None,
        interactive: bool = True,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        instructions: str | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
    ) -> AgentRunResult[FakeRunOutput]:
        self.turns.append(
            RecordedRun(
                prompt=user_prompt,
                history=list(message_history or []),
                address=conversation_address,
                thread_id=thread_id,
                source_thread_address=source_thread_address,
                source_thread_message_ids=list(source_thread_message_ids or []),
                run_name=run_name,
                deferred_results=deferred_tool_results,
                model=model,
                effort=effort,
                conversation_id=conversation_id,
                interactive=interactive,
                instructions=instructions,
                capabilities=list(capabilities or []),
            )
        )
        if not self.allow_reception_run:
            raise AssertionError("reception should use run_stream_events")
        recorded_teleport = self.reception_recorded_teleport
        if recorded_teleport is not None and deferred_tool_results is None:
            # Consumed on first use, or the resumed run would teleport forever.
            self.reception_recorded_teleport = None
            gateway = next(
                (
                    capability
                    for capability in capabilities or []
                    if isinstance(capability, GatewayCapability)
                ),
                None,
            )
            if gateway is None:
                raise AssertionError("a recorded teleport requires a mounted gateway")
            await gateway.session.teleport(hint=recorded_teleport)
            return AgentRunResult(self.reception_output)
        if self.reception_teleport is not None and deferred_tool_results is None:
            output: FakeRunOutput = _teleport_requests(
                self.reception_teleport, self.reception_teleport_destination
            )
        else:
            output = self.reception_output
        scheme_decision = self.reception_scheme
        if scheme_decision is not None:
            # Cast once, like a model would: the receiving run in the DM must not
            # re-cast it, where the gate would (rightly) refuse.
            self.reception_scheme = None
            await _gate_tool(capabilities, SCHEME_TOOL_NAME)(
                cast(RunContext[None], None),
                hint=scheme_decision.hint,
                brief=scheme_decision.brief,
                destination=scheme_target(scheme_decision, capabilities),
            )
            # And a closing line, like a model does: `scheme` is an ordinary tool
            # call, so the run carries on and writes a reply after it. Set
            # `reception_output=""` for one that moves without a word.
            return AgentRunResult(self.reception_output)
        summon_decision = self.reception_summon
        if summon_decision is not None:
            summon = next(
                (
                    capability
                    for capability in capabilities or []
                    if isinstance(capability, GatewayCapability)
                ),
                None,
            )
            if summon is None:
                raise AssertionError("summon decision requires SummonCapability")
            if summon.toolset is None:
                raise AssertionError("summon capability requires a toolset")
            summon_tool = summon.toolset.tools[SUMMON_TOOL_NAME].function
            await summon_tool(
                cast(RunContext[None], None),
                agent_id=summon_decision.agent_id,
                model=summon_decision.model,
                destination=summon_target(summon_decision),
                reason=summon_decision.reason,
                hint=summon_decision.hint,
                summon=summon_decision.summon,
            )
            output = ""
        if isinstance(output, DeferredToolRequests) and deferred_suspender is not None:
            await deferred_suspender.suspend(output)
        return AgentRunResult(output)

    def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        model: Model | str | None = None,
        effort: ThinkingEffort | None = None,
        conversation_id: uuid.UUID | None = None,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
    ) -> ReactEventStream[ChannelOutput]:
        self.streams.append(
            RecordedRun(
                prompt=user_prompt,
                history=list(message_history or []),
                address=conversation_address,
                thread_id=thread_id,
                source_thread_address=source_thread_address,
                source_thread_message_ids=list(source_thread_message_ids or []),
                run_name=run_name,
                deferred_results=deferred_tool_results,
                model=model,
                effort=effort,
                conversation_id=conversation_id,
                capabilities=list(capabilities or []),
            )
        )
        scheme_decision = self.reception_scheme
        if scheme_decision is not None:
            # Cast once, like a model would — see the non-streaming path.
            self.reception_scheme = None

            async def scheme_events() -> AsyncGenerator[
                ReactStreamEvent[ChannelOutput], None
            ]:
                await _gate_tool(capabilities, SCHEME_TOOL_NAME)(
                    cast(RunContext[None], None),
                    hint=scheme_decision.hint,
                    brief=scheme_decision.brief,
                    destination=scheme_target(scheme_decision, capabilities),
                )
                yield AgentRunResultEvent(AgentRunResult(""))

            return ReactEventStream(scheme_events())

        summon_decision = self.reception_summon
        if summon_decision is not None:

            async def summon_events() -> AsyncGenerator[
                ReactStreamEvent[ChannelOutput], None
            ]:
                summon = next(
                    (
                        capability
                        for capability in capabilities or []
                        if isinstance(capability, GatewayCapability)
                    ),
                    None,
                )
                if summon is None:
                    raise AssertionError("summon decision requires SummonCapability")
                if summon.toolset is None:
                    raise AssertionError("summon capability requires a toolset")
                summon_tool = summon.toolset.tools[SUMMON_TOOL_NAME].function
                await summon_tool(
                    cast(RunContext[None], None),
                    agent_id=summon_decision.agent_id,
                    model=summon_decision.model,
                    destination=summon_target(summon_decision),
                    reason=summon_decision.reason,
                    hint=summon_decision.hint,
                    summon=summon_decision.summon,
                )
                yield AgentRunResultEvent(AgentRunResult(""))

            return ReactEventStream(summon_events())

        output: ChannelOutput | DeferredToolRequests
        if self.reception_teleport is not None and deferred_tool_results is None:
            output = _teleport_requests(self.reception_teleport)
        else:
            output = self.reception_output
        if isinstance(output, DeferredToolRequests):

            async def deferred_events() -> AsyncGenerator[
                ReactStreamEvent[ChannelOutput], None
            ]:
                if deferred_suspender is not None:
                    event = await deferred_suspender.suspend(output)
                    if event is not None:
                        yield event
                yield AgentRunResultEvent(AgentRunResult(output))

            return ReactEventStream(deferred_events())

        if not isinstance(output, str):
            raise AssertionError("streamed reception output must be text or summon")
        script = self.reception_script or plain_answer(output)
        return ReactEventStream(play(script))


@dataclass
class ScriptedTurn:
    """One turn of the conversation: one tool call to emit as a streamed delta."""

    tool_name: str
    args: JsonObject
    tool_call_id: str


@dataclass
class ScriptedStream:
    """FunctionModel stream callback: emits tool-call deltas or text output."""

    turns: list[ScriptedTurn | str]
    cursor: int = 0
    seen_output_tools: list[list[str]] = field(default_factory=list)
    __name__: str = "scripted_stream"

    def __call__(
        self, messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        self.seen_output_tools.append([t.name for t in info.output_tools])
        turn = self.turns[self.cursor]
        self.cursor += 1
        return emit_scripted_turn(turn)


async def emit_scripted_turn(
    turn: ScriptedTurn | str,
) -> AsyncIterator[str | DeltaToolCalls]:
    if isinstance(turn, str):
        yield turn
        return
    yield {
        0: DeltaToolCall(
            name=turn.tool_name,
            json_args=json.dumps(turn.args),
            tool_call_id=turn.tool_call_id,
        )
    }


def build_scripted_agent(
    turns: list[ScriptedTurn | str],
) -> tuple[Agent[None, ScriptedOutput], ScriptedStream]:
    script = ScriptedStream(turns=turns)
    agent: Agent[None, ScriptedOutput] = Agent(
        FunctionModel(stream_function=script, model_name="scripted"),
        deps_type=type(None),
        output_type=[str, DeferredToolRequests],
        capabilities=[AskCapability()],
        system_prompt=SYSTEM_PROMPT,
    )
    return agent, script


def build_non_stream_agent() -> Agent[None, ScriptedOutput]:
    def respond(
        messages: list[ModelMessage],
        info: AgentInfo,
    ) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content="all done!")])

    return Agent(
        FunctionModel(function=respond, model_name="scripted"),
        deps_type=type(None),
        output_type=[str, DeferredToolRequests],
        capabilities=[AskCapability()],
        system_prompt=SYSTEM_PROMPT,
    )


class RecordingClaudeClient:
    """`ClaudeSDKClient` stand-in that records the options a Claude run was launched
    with and drives no tools, so a test can read what the run asked the CLI for."""

    last_options: ClassVar[ClaudeAgentOptions | None] = None

    def __init__(
        self, options: ClaudeAgentOptions | None = None, transport: object = None
    ) -> None:
        RecordingClaudeClient.last_options = options

    async def __aenter__(self) -> RecordingClaudeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def query(self, prompt: str) -> None:
        return None

    async def receive_response(self) -> AsyncIterator[ClaudeMessage]:
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s1",
            result="done",
        )
