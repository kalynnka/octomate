"""The canonical fake agents.

`FakeAgent` stands in for an `AgentTentacle` at the tentacle level: it records
run/stream calls and plays scripted outputs (a channel answer, deferred
requests, or a scenarios event script). The `Scripted*` builders operate at the
model level instead — they build real pydantic-ai agents around a scripted
`FunctionModel`, for tests that drive the actual react graph.
"""

from __future__ import annotations

import json
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Sequence,
)
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import ClassVar, cast

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage
from claude_agent_sdk.types import Message as ClaudeMessage
from octomate_protocol.gateway import GatewayTool
from pydantic import UUID7
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
)
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.triage import (
    DIRECT_TARGET,
    TELEPORT_DEFER_KIND,
    Claim,
    DirectTarget,
    SchemeDecision,
    SchemeTarget,
    SummonDecision,
)
from octomate.tentacles.agent import AgentTentacle
from octomate.tentacles.channel import ChannelOutput
from octomate.tentacles.inkling.prompts import SYSTEM_PROMPT
from octomate.types.json import JsonObject
from tests.support.scenarios import plain_answer, play

FakeRunOutput = ChannelOutput
ScriptedOutput = str | DeferredToolRequests


def _teleport_requests(
    hint: str,
    here: ChannelAddress,
    destination: str = "thread",
    project: str | None = None,
    *,
    resume: bool,
) -> DeferredToolRequests:
    """A reception run's `teleport` deferral — the suspender skips it and the graph
    forks + resumes. On the resumed run (deferred results present) the fake answers
    normally, so a teleport turn does not loop.

    `destination` is the handle a model would name, and the metadata is what the gate
    puts there once it has resolved one: a crossing carries the far channel and the
    account on it, and `thread` or `here` the current conversation."""
    crossing = destination not in ("thread", "here")
    return DeferredToolRequests(
        calls=[
            ToolCallPart(
                tool_name=GatewayTool.TELEPORT,
                args={"hint": hint, "destination": destination, "project": project},
                tool_call_id="call_teleport",
            )
        ],
        metadata={
            "call_teleport": {
                "kind": TELEPORT_DEFER_KIND,
                "hint": hint,
                "destination": asdict(
                    ChannelAddress(
                        channel_tentacle_id=destination,
                        chat_type="dm",
                        chat_id="",
                        user_id="ou_alice",
                    )
                    if crossing
                    else here
                ),
                "new_thread": destination != "here",
                "project": project or "",
                "ref": "",
                "resume": resume,
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
    return DirectTarget(channel=far)


@dataclass
class RecordedRun:
    prompt: str | Sequence[UserContent] | None
    history: list[ModelMessage]
    address: ChannelAddress
    run_name: str | None
    thread_id: UUID7 | None = None
    source_thread_address: ChannelAddress | None = None
    source_thread_message_ids: list[UUID7] = field(default_factory=list)
    deferred_results: DeferredToolResults | None = None
    model: Model | str | None = None
    effort: str | None = None
    conversation_id: UUID7 | None = None
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
    # The project the teleport carries, binding the thread landed in.
    reception_teleport_project: str | None = None
    # Whether the teleport asks to carry on at once where it lands.
    reception_teleport_resume: bool = True
    # When set, the first reception run records a teleport decision on the mounted
    # gateway's session — the way an external runtime's MCP tool does — and ends
    # its turn as the same deferral, the way that runtime's tentacle does once
    # interrupted on it; the resumed run gets `reception_output`.
    reception_recorded_teleport: str | None = None
    # When set, the reception run casts `dismiss` through the mounted gateway, the
    # way a model does mid-run, and carries on to `reception_output`.
    reception_dismiss: bool = False
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
            model: Claim(
                ability="fake agent",
                efforts=("minimal", "low", "medium", "high", "xhigh"),
            )
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
    # Every `relocate` the graph asked for: the conversation's id and where to.
    relocated: list[tuple[UUID7, Path]] = field(default_factory=list)

    async def fork_session(
        self, conversation: Conversation, *, cwd: Path
    ) -> str | None:
        """The fake has no external runtime session to fork."""
        return None

    async def relocate(self, conversation: Conversation, *, cwd: Path) -> None:
        self.relocated.append((conversation.id, cwd))

    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: UUID7 | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[UUID7] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[FakeRunOutput] | None = None,
        model: Model | str | None = None,
        effort: str | None = None,
        conversation_id: UUID7 | None = None,
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
            decision = await gateway.session.teleport(
                hint=recorded_teleport, resume=self.reception_teleport_resume
            )
            deferral = decision.deferral("call_teleport")
            if deferred_suspender is not None:
                await deferred_suspender.suspend(deferral)
            return AgentRunResult(deferral)
        if self.reception_teleport is not None and deferred_tool_results is None:
            output: FakeRunOutput = _teleport_requests(
                self.reception_teleport,
                conversation_address,
                self.reception_teleport_destination,
                self.reception_teleport_project,
                resume=self.reception_teleport_resume,
            )
        else:
            output = self.reception_output
        if self.reception_dismiss:
            await _gate_tool(capabilities, GatewayTool.DISMISS)(
                cast(RunContext[None], None)
            )
        scheme_decision = self.reception_scheme
        if scheme_decision is not None:
            # Cast once, like a model would: the receiving run in the DM must not
            # re-cast it, where the gate would (rightly) refuse.
            self.reception_scheme = None
            await _gate_tool(capabilities, GatewayTool.SCHEME)(
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
            summon_tool = summon.toolset.tools[GatewayTool.SUMMON].function
            await summon_tool(
                cast(RunContext[None], None),
                agent_id=summon_decision.agent_id,
                model=summon_decision.model,
                reason=summon_decision.reason,
                hint=summon_decision.hint,
                brief=summon_decision.brief,
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
        thread_id: UUID7 | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[UUID7] | None = None,
        run_name: str | None = None,
        model: Model | str | None = None,
        effort: str | None = None,
        conversation_id: UUID7 | None = None,
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
                await _gate_tool(capabilities, GatewayTool.SCHEME)(
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
                summon_tool = summon.toolset.tools[GatewayTool.SUMMON].function
                await summon_tool(
                    cast(RunContext[None], None),
                    agent_id=summon_decision.agent_id,
                    model=summon_decision.model,
                    reason=summon_decision.reason,
                    hint=summon_decision.hint,
                    brief=summon_decision.brief,
                )
                yield AgentRunResultEvent(AgentRunResult(""))

            return ReactEventStream(summon_events())

        output: ChannelOutput | DeferredToolRequests
        if self.reception_teleport is not None and deferred_tool_results is None:
            output = _teleport_requests(
                self.reception_teleport,
                conversation_address,
                self.reception_teleport_destination,
                self.reception_teleport_project,
                resume=self.reception_teleport_resume,
            )
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
