from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import httpx
from pydantic_ai import Agent, CallDeferred, RunContext
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from octomate.config import ModelConfig, PulseConfig
from octomate.schemas.actions import AgentMessage
from octomate.schemas.events import MessageEvent
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.agent.context import RetryTransport, SessionContext
from octomate.tentacles.agent.pulse.graph import Triage, pulse_graph
from octomate.tentacles.agent.pulse.prompts import STEP_PROMPT, SYSTEM_PROMPT
from octomate.tentacles.agent.pulse.state import (
    LocalSubAgent,
    PulseDeps,
    PulseState,
    SubAgent,
    TriageOutput,
)
from octomate.tentacles.agent.skills import SkillManager
from octomate.transmuters.interactions import Todo

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

    from octomate.octopus import Octopus
    from octomate.tentacles.channel.base import StreamSink

logger = logging.getLogger(__name__)


def _build_model(config: ModelConfig, http_client: httpx.AsyncClient) -> GoogleModel:
    kwargs: dict[str, Any] = {"http_client": http_client}
    if config.base_url:
        kwargs["base_url"] = config.base_url
    if config.vertexai:
        kwargs["vertexai"] = True
        if config.project:
            kwargs["project"] = config.project
        if config.location:
            kwargs["location"] = config.location
    else:
        if config.api_key:
            kwargs["api_key"] = config.api_key
    return GoogleModel(config.model, provider=GoogleProvider(**kwargs))


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


class PulseTentacle(AgentTentacle):
    """Octomate's pulse — merged triage/synthesize + subagent execution.

    Channels invoke process() directly (not via beckon) to run the pulse graph.
    Can also be beckoned as an AgentTentacle via run() for silent plan steps.
    """

    pulse_agent: Agent[SessionContext, TriageOutput]
    subagents: dict[str, SubAgent]
    triage_toolsets: Sequence[AbstractToolset[SessionContext]] | None
    subagent_catalog: str | None

    def __init__(
        self,
        octopus: Octopus,
        config: PulseConfig,
        skill_manager: SkillManager | None = None,
    ) -> None:
        super().__init__(
            "pulse",
            octopus,
            description="Pulse — triage, planning, and step execution",
            handover=False,
        )

        http_client = httpx.AsyncClient(
            transport=RetryTransport(httpx.AsyncHTTPTransport()),
            timeout=httpx.Timeout(180.0),
        )

        main_model = _build_model(config.main, http_client)
        main_settings = (
            ModelSettings(thinking=config.main.thinking)
            if config.main.thinking
            else None
        )

        skill_toolsets = skill_manager.build_skillsets() if skill_manager else []
        summon_toolset = build_summon_toolset(octopus.agent_tentacles)
        self.triage_toolsets = [
            *skill_toolsets,
            *([summon_toolset] if summon_toolset else []),
        ] or None

        self.pulse_agent = Agent(
            main_model,
            system_prompt=SYSTEM_PROMPT,
            deps_type=SessionContext,
            output_type=[list[AgentMessage], list[Todo], DeferredToolRequests],
            toolsets=self.triage_toolsets,
            model_settings=main_settings,
        )

        self.subagents = {}
        for tag, sub_cfg in config.subagents.items():
            sub_model = _build_model(sub_cfg, http_client)
            sub_settings = (
                ModelSettings(thinking=sub_cfg.thinking) if sub_cfg.thinking else None
            )
            self.subagents[tag] = LocalSubAgent(
                id=tag,
                description=sub_cfg.description or f"Pulse subagent {tag}",
                agent=Agent(
                    sub_model,
                    system_prompt=sub_cfg.system_prompt or STEP_PROMPT,
                    deps_type=SessionContext,
                    output_type=str,
                    model_settings=sub_settings,
                ),
            )

        if self.subagents:
            lines = [
                f'- "{tag}": {sub.description}' for tag, sub in self.subagents.items()
            ]
            self.subagent_catalog = (
                "[available step assignees (subagents)]\n" + "\n".join(lines)
            )
        else:
            self.subagent_catalog = None

    async def process(
        self,
        key: Any,
        state: PulseState,
        session_ctx: SessionContext,
        *,
        memory_instructions: str | None = None,
        message_history: list[ModelMessage] | None = None,
        stream: StreamSink | None = None,
    ) -> list[AgentMessage]:
        """Run the pulse graph and return the final messages. Called directly by channels."""
        instructions = (
            "\n\n".join(filter(None, [self.subagent_catalog, memory_instructions]))
            or None
        )

        pulse_deps = PulseDeps(
            pulse_agent=self.pulse_agent,
            subagents=self.subagents,
            tentacles={
                k: v for k, v in self.octopus.agent_tentacles.items() if k != self.id
            },
            agent_deps=session_ctx,
            tentacle=session_ctx.tentacle,
            toolsets=session_ctx.tentacle.toolsets if session_ctx.tentacle else None,
            instructions=instructions,
            message_history=message_history,
            stream=stream,
        )
        graph_result = await pulse_graph.run(Triage(), state=state, deps=pulse_deps)
        return graph_result.output

    async def run(
        self,
        key: Any,
        contents: list[Any],
        *,
        session_name: str = "",
        silent: bool = False,
    ) -> str | None:
        """AgentTentacle beckoning path — runs graph, twitches result if not silent."""

        channel = self.octopus.tentacles.get(key.tentacle_id)
        if channel is None:
            return None

        user_prompt: list = []
        for msg in contents:
            if isinstance(msg, MessageEvent):
                user_prompt.extend(msg.to_content_parts())

        memories = await channel.memory.recall(key, contents, channel)
        memory_instructions: str | None = None
        if memories:
            facts = "\n".join(f"- {m}" for m in memories)
            memory_instructions = f"[relevant memories]\n{facts}"

        message_history = await channel.memory.history(key, size=32)
        session_ctx = SessionContext(session_key=key, tentacle=channel)
        state = PulseState(prompt=user_prompt)

        messages = await self.process(
            key,
            state,
            session_ctx,
            memory_instructions=memory_instructions,
            message_history=message_history,
            stream=None,
        )

        if not silent:
            for msg in messages:
                await channel.twitch(key, msg.segments)
            asyncio.create_task(channel.memory.memo(key, messages, channel))

        return "\n".join(str(seg) for msg in messages for seg in msg.segments)
