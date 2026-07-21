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
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import cast

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
from octomate.capabilities.agent import Agent
from octomate.capabilities.deferred import DeferredSuspender
from octomate.capabilities.react import ReactEventStream, ReactStreamEvent
from octomate.capabilities.gate import (
    SUMMON_TOOL_NAME,
    TELEPORT_KIND,
    TELEPORT_TOOL_NAME,
    GatewayCapability,
)
from octomate.schemas.conversation import ChannelAddress
from pydantic_ai.settings import ThinkingEffort

from octomate.schemas.triage import SummonDecision
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.agent.inkling import inkling_toolset
from octomate.tentacles.agent.inkling.prompts import SYSTEM_PROMPT
from octomate.tentacles.channel.base import ChannelOutput
from octomate.types.json import JsonObject

from tests.support.scenarios import plain_answer, play

FakeRunOutput = ChannelOutput
ScriptedOutput = str | DeferredToolRequests


def _teleport_requests(hint: str) -> DeferredToolRequests:
    """A reception run's `teleport` deferral — the suspender skips it and the graph
    forks + resumes. On the resumed run (deferred results present) the fake answers
    normally, so a teleport turn does not loop."""
    return DeferredToolRequests(
        calls=[
            ToolCallPart(
                tool_name=TELEPORT_TOOL_NAME,
                args={"hint": hint},
                tool_call_id="call_teleport",
            )
        ],
        metadata={"call_teleport": {"kind": TELEPORT_KIND, "hint": hint}},
    )


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
    # When set, the first reception run emits a `teleport` deferral with this hint;
    # the resumed run (deferred results present) falls through to `reception_output`.
    reception_teleport: str | None = None
    reception_script: list[ReactStreamEvent[ChannelOutput]] | None = None
    allow_reception_run: bool = False
    models: dict[str, Model | str] = field(
        default_factory=lambda: {
            "test": "fake-model",
            "deepseek:deepseek-v4-flash": "fake-model",
            "deepseek:deepseek-v4-pro": "fake-model",
            "opus": "fake-model",
            "opusplan[1m]": "fake-model",
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
                capabilities=list(capabilities or []),
            )
        )
        if not self.allow_reception_run:
            raise AssertionError("reception should use run_stream_events")
        if self.reception_teleport is not None and deferred_tool_results is None:
            output: FakeRunOutput = _teleport_requests(self.reception_teleport)
        else:
            output = self.reception_output
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
                destination=summon_decision.destination,
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
                capabilities=list(capabilities or []),
            )
        )
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
                    destination=summon_decision.destination,
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
        toolsets=[inkling_toolset],
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
        toolsets=[inkling_toolset],
        system_prompt=SYSTEM_PROMPT,
    )
