"""Pulse — planning graph and agent factory for Octomate.

Uses pydantic-ai's graph to triage requests, decompose eligible ones into
Todo-backed steps, execute each step, and synthesize a final response.
Simple questions are answered directly without planning.

Deferred tool calls (summon, ask_user, approval) are resolved inside graph
nodes via the ChannelTentacle accessible through ``PulseDeps``.

Usage::

    result = await pulse_graph.run(Triage(), state=state, deps=pulse_deps)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple, TypeVar

import httpx
from pydantic_ai import (
    Agent,
    AgentRunResult,
    CallDeferred,
    DeferredToolResults,
    RunContext,
)
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import FunctionToolset
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from octomate.agents.base import RetryTransport, SessionContext
from octomate.agents.manager import SKILL_METADATA_KEY, SkillManager
from octomate.agents.prompts import BASE_PROMPT, FLICK_EXTRA
from octomate.config import FlickConfig
from octomate.schemas.actions import AgentMessage
from octomate.schemas.segments import TextSegment
from octomate.transmuters.interactions import Todo

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

    from octomate.tentacles.base import AgentTentacle, ChannelTentacle

AgentOutputT = TypeVar("AgentOutputT")

SYSTEM_PROMPT = BASE_PROMPT + FLICK_EXTRA

TriageOutput = list[AgentMessage] | list[Todo] | DeferredToolRequests


class PulseAgents(NamedTuple):
    triage: Agent[SessionContext, TriageOutput]
    step: Agent[SessionContext, str]
    synthesize: Agent[SessionContext, list[AgentMessage]]


def build_summon_toolset(
    agent_tentacles: dict[str, AgentTentacle],
) -> FunctionToolset[SessionContext] | None:
    if not agent_tentacles:
        return None

    lines = []
    for t in agent_tentacles.values():
        mode = (
            "handover (takes over the thread for continuous interaction)"
            if t.handover
            else "fire-and-forget (dispatches and returns)"
        )
        lines.append(f'- "{t.id}" [{mode}]: {t.description}')
    descriptions = "\n".join(lines)
    tool_description = (
        "Summon an agent tentacle for deep processing.\n\n"
        "Use when user explicitly requests, or requires coding, research, or complex reasoning.\n"
        "Write a clear summary capturing the user's actual request and context.\n"
        "The agent only sees this summary — not the raw chat history.\n\n"
        "Modes:\n"
        "- handover: The agent takes over the thread. All follow-up messages go to it until it finishes.\n"
        "- fire-and-forget: The agent runs the task in the background. You keep the conversation.\n\n"
        f"Available agent tentacles:\n{descriptions}"
    )

    toolset = FunctionToolset[SessionContext]()

    @toolset.tool(requires_approval=False, description=tool_description)
    async def summon(
        ctx: RunContext[SessionContext],
        tentacle_tag: str,
        summary: str,
        user_prefer: str,
        language: str,
    ) -> str:
        raise CallDeferred()

    return toolset


def create_pulse_agents(
    config: FlickConfig,
    skill_manager: SkillManager | None = None,
    summon_toolset: FunctionToolset[SessionContext] | None = None,
) -> PulseAgents:
    http_client = httpx.AsyncClient(
        transport=RetryTransport(httpx.AsyncHTTPTransport()),
        timeout=httpx.Timeout(10.0),
    )
    provider = GoogleProvider(
        base_url=config.base_url or None,
        api_key=config.api_key or None,
        http_client=http_client,
    )
    model = GoogleModel(config.model, provider=provider)

    skill_toolsets = skill_manager.build_skillsets() if skill_manager else []
    triage_toolsets = [*skill_toolsets]
    if summon_toolset:
        triage_toolsets.append(summon_toolset)

    triage = Agent(
        model,
        system_prompt=SYSTEM_PROMPT,
        deps_type=SessionContext,
        output_type=[list[AgentMessage], list[Todo], DeferredToolRequests],
        toolsets=triage_toolsets or None,
    )
    step = Agent(
        model,
        system_prompt=BASE_PROMPT,
        deps_type=SessionContext,
        output_type=str,
        toolsets=skill_toolsets or None,
    )
    synthesize = Agent(
        model,
        system_prompt=BASE_PROMPT,
        deps_type=SessionContext,
        output_type=list[AgentMessage],
    )

    return PulseAgents(triage=triage, step=step, synthesize=synthesize)


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

TRIAGE_INSTRUCTION = (
    "If the request below is a simple question, answer it directly.\n"
    "If it requires multiple steps, produce a structured plan of 2-5 Todo "
    "items, each with a todo_id (like 'pulse-0'), a short title, and a "
    "detailed description with instructions for that step."
)

STEP_INSTRUCTION = (
    "Complete ONLY the step described below and return ONLY the result. "
    "Do not mention the plan, other steps, or any meta-commentary.\n\n"
    "Overall goal: {goal}\n"
    "Previous context:\n{context}\n\n"
    "Current step: {title}\n"
    "Instructions: {description}"
)

SYNTHESIZE_INSTRUCTION = (
    "Using the collected results below, write a single coherent response that "
    "directly answers the original request. Do not mention steps, plans, or "
    "any internal process.\n\n"
    "Step results:\n{results}"
)


@dataclass
class PulseState:
    prompt: str | list
    todos: list[Todo] = field(default_factory=list)
    step_outputs: list[str] = field(default_factory=list)

    @property
    def goal(self) -> str:
        if isinstance(self.prompt, str):
            return self.prompt
        return " ".join(str(p) for p in self.prompt)


@dataclass
class PulseDeps:
    triage: Agent[SessionContext, TriageOutput]
    step: Agent[SessionContext, str]
    synthesize: Agent[SessionContext, list[AgentMessage]]
    agent_deps: SessionContext
    tentacle: ChannelTentacle
    toolsets: list[FunctionToolset[SessionContext]] | None = None
    instructions: str | None = None
    message_history: list[ModelMessage] | None = None


async def resolve_deferred(
    agent: Agent[SessionContext, AgentOutputT],
    result: AgentRunResult[AgentOutputT],
    tentacle: ChannelTentacle,
    deps: SessionContext,
    toolsets: list[FunctionToolset[SessionContext]] | None = None,
) -> AgentRunResult[AgentOutputT]:
    """Resolve deferred tool calls (HITL interactions) until the agent produces output.

    Handles summon (dispatches via nerve, handover sends a notification to the
    channel), ask_user, and tool approval flows using the tentacle's feelers.
    """
    from octomate.nerve import SummonAgent
    from octomate.schemas.events import MessageEvent

    key = deps.session_key
    while isinstance(result.output, DeferredToolRequests):
        deferred = DeferredToolResults()

        for call in result.output.calls:
            if call.tool_name == "summon":
                args = call.args_as_dict()
                tag = args.get("tentacle_tag", "")
                summary = args.get("summary", "")
                agent_tentacle = tentacle.octopus.agent_tentacles.get(tag)
                if agent_tentacle is None:
                    deferred.calls[call.tool_call_id] = f"Unknown agent tentacle: {tag}"
                    continue

                content = MessageEvent(
                    tentacle_id=key.tentacle_id,
                    user_id=key.user_id,
                    chat_id=key.chat_id,
                    chat_type="group" if key.group_id else "private",
                    segments=[TextSegment(data={"text": summary})],
                )
                await tentacle.octopus.agent_nerve.send(
                    SummonAgent(
                        key=key,
                        agent_tag=tag,
                        contents=[content],
                        summary=summary,
                    )
                )

                if agent_tentacle.handover:
                    await tentacle.twitch(
                        key,
                        [TextSegment(data={"text": f"Tentacle *{tag}* has grabbed this thread 🐙!"})],
                    )
                    deferred.calls[call.tool_call_id] = (
                        f"Thread handed over to agent '{tag}'. "
                        f"Do NOT produce any further response."
                    )
                else:
                    deferred.calls[call.tool_call_id] = (
                        f"Task dispatched to agent '{tag}'. "
                        f"It will reply in this thread when done."
                    )
            elif call.tool_name == "ask_user":
                args = call.args_as_dict()
                resp = await tentacle.feelers.questions.ask_question(
                    key,
                    args.get("question", ""),
                    session_key=key,
                    options=args.get("options"),
                )
                deferred.calls[call.tool_call_id] = (
                    resp.answer if resp else "(no response)"
                )

        for call in result.output.approvals:
            tool_meta = result.output.metadata.get(call.tool_call_id, {})

            thread = await tentacle.threads.get(key)
            if tentacle.feelers.confirm.is_session_allowed(
                str(thread.id), call.tool_name
            ):
                deferred.approvals[call.tool_call_id] = True
                continue

            action, future = await tentacle.feelers.confirm.create_confirmation(
                key=key,
                tool_name=call.tool_name,
                tool_call_id=call.tool_call_id,
                args=call.args_as_dict(),
                title=tool_meta.get("description", call.tool_name),
                description=tool_meta.get("description", ""),
                skill=tool_meta.get(SKILL_METADATA_KEY, ""),
                approvers=tool_meta.get("approvers"),
            )

            sent = await tentacle.feelers.confirm.send_confirmation(key, action)
            if not sent:
                await tentacle.feelers.confirm.expire_confirmation(
                    action.confirmation_id
                )
                deferred.approvals[call.tool_call_id] = False
                continue

            try:
                approved = await asyncio.wait_for(
                    future, timeout=tentacle.feelers.confirm.timeout
                )
            except TimeoutError:
                await tentacle.feelers.confirm.expire_confirmation(
                    action.confirmation_id
                )
                await tentacle.feelers.confirm.send_timeout_notification(key, action)
                approved = False
            except asyncio.CancelledError:
                await tentacle.feelers.confirm.expire_confirmation(
                    action.confirmation_id
                )
                await tentacle.feelers.confirm.send_timeout_notification(key, action)
                raise

            deferred.approvals[call.tool_call_id] = approved

        result = await agent.run(
            message_history=result.all_messages(),
            deferred_tool_results=deferred,
            deps=deps,
            toolsets=toolsets,
        )

    return result


@dataclass
class Triage(BaseNode[PulseState, PulseDeps, list[AgentMessage]]):
    """Classify the request: answer directly, produce a plan, or summon an agent."""

    async def run(
        self, ctx: GraphRunContext[PulseState, PulseDeps]
    ) -> ExecuteStep | End[list[AgentMessage]]:
        triage_instructions = TRIAGE_INSTRUCTION
        if ctx.deps.instructions:
            triage_instructions = f"{ctx.deps.instructions}\n\n{TRIAGE_INSTRUCTION}"

        result = await ctx.deps.triage.run(
            ctx.state.prompt,
            deps=ctx.deps.agent_deps,
            toolsets=ctx.deps.toolsets,
            instructions=triage_instructions,
            message_history=ctx.deps.message_history,
        )
        if isinstance(result.output, DeferredToolRequests):
            result = await resolve_deferred(
                ctx.deps.triage,
                result,
                ctx.deps.tentacle,
                ctx.deps.agent_deps,
                ctx.deps.toolsets,
            )

        output = result.output
        if isinstance(output, list) and output and isinstance(output[0], Todo):
            ctx.state.todos = [t for t in output if isinstance(t, Todo)]
            return ExecuteStep()
        return End(output)  # type: ignore[arg-type]


@dataclass
class ExecuteStep(BaseNode[PulseState, PulseDeps]):
    """Execute the next pending Todo step."""

    async def run(
        self, ctx: GraphRunContext[PulseState, PulseDeps]
    ) -> ExecuteStep | Synthesize:
        current = next((t for t in ctx.state.todos if t.status == "pending"), None)
        if current is None:
            return Synthesize()

        context = (
            "\n".join(ctx.state.step_outputs) if ctx.state.step_outputs else "(none)"
        )
        step_instructions = STEP_INSTRUCTION.format(
            goal=ctx.state.goal,
            context=context,
            title=current.title,
            description=current.description,
        )
        result = await ctx.deps.step.run(
            current.title,
            deps=ctx.deps.agent_deps,
            message_history=ctx.deps.message_history,
            instructions=step_instructions,
        )
        if isinstance(result.output, DeferredToolRequests):
            result = await resolve_deferred(
                ctx.deps.step,
                result,
                ctx.deps.tentacle,
                ctx.deps.agent_deps,
            )
        ctx.state.step_outputs.append(f"- {current.title}: {result.output}")

        ctx.state.todos = [
            t.model_copy(update={"status": "done"})
            if t.todo_id == current.todo_id
            else t
            for t in ctx.state.todos
        ]
        return ExecuteStep()


@dataclass
class Synthesize(BaseNode[PulseState, PulseDeps, list[AgentMessage]]):
    """Produce the final user-facing answer from all step outputs."""

    async def run(
        self, ctx: GraphRunContext[PulseState, PulseDeps]
    ) -> End[list[AgentMessage]]:
        results_block = "\n".join(ctx.state.step_outputs)
        synth_instructions = SYNTHESIZE_INSTRUCTION.format(results=results_block)
        result = await ctx.deps.synthesize.run(
            ctx.state.goal,
            deps=ctx.deps.agent_deps,
            message_history=ctx.deps.message_history,
            instructions=synth_instructions,
        )
        return End(result.output)


pulse_graph = Graph(nodes=[Triage, ExecuteStep, Synthesize])
