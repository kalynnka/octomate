from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic_ai import Agent
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import AbstractToolset

from octomate.schemas.actions import AgentMessage
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.agent.context import SessionContext
from octomate.transmuters.interactions import Todo

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

    from octomate.tentacles.channel.base import ChannelTentacle, StreamSink

TriageOutput = list[AgentMessage] | list[Todo] | DeferredToolRequests


@runtime_checkable
class SubAgent(Protocol):
    """Pure-compute subagent: takes a task, returns a string. No HITL, no deferred tools."""

    id: str
    description: str

    async def execute(
        self,
        key: Any,
        current: Todo,
        state: PulseState,
        deps: SessionContext,
        *,
        stream: StreamSink | None = None,
        message_history: list[ModelMessage] | None = None,
    ) -> str: ...


@dataclass
class LocalSubAgent:
    id: str
    description: str
    agent: Agent[SessionContext, str]

    async def execute(
        self,
        key: Any,
        current: Todo,
        state: PulseState,
        deps: SessionContext,
        *,
        stream: StreamSink | None = None,
        message_history: list[ModelMessage] | None = None,
    ) -> str:
        from octomate.tentacles.agent.pulse.prompts import STEP_INSTRUCTION
        from octomate.tentacles.agent.pulse.run import streaming

        completed = {tid: out for tid, out in state.step_outputs.items()}
        context = (
            "\n".join(
                f"- {t.title}: {completed[t.todo_id]}"
                for t in state.todos
                if t.todo_id in completed
            )
            or "(none)"
        )
        instructions = STEP_INSTRUCTION.format(
            goal=state.goal,
            context=context,
            title=current.title,
            description=current.description,
        )
        result = await streaming(
            self.agent,
            stream,
            user_prompt=current.title,
            deps=deps,
            message_history=message_history,
            instructions=instructions,
        )
        if isinstance(result.output, DeferredToolRequests):
            raise RuntimeError(
                f"SubAgent {self.id!r} emitted deferred tool calls — "
                "subagents must be configured with non-deferring toolsets only"
            )
        return result.output


@dataclass
class PulseState:
    prompt: str | list
    todos: list[Todo] = field(default_factory=list)
    step_outputs: dict[str, str] = field(default_factory=dict)
    main_history: list[ModelMessage] | None = None
    inline_todo_ids: set[str] = field(default_factory=set)
    card_ref: str | None = None

    @property
    def goal(self) -> str:
        if isinstance(self.prompt, str):
            return self.prompt
        return " ".join(str(p) for p in self.prompt)


@dataclass
class PulseDeps:
    pulse_agent: Agent[SessionContext, TriageOutput]
    subagents: dict[str, SubAgent]
    tentacles: dict[str, AgentTentacle]
    agent_deps: SessionContext
    tentacle: ChannelTentacle
    toolsets: Sequence[AbstractToolset[SessionContext]] | None = None
    instructions: str | None = None
    message_history: list[ModelMessage] | None = None
    stream: StreamSink | None = None
