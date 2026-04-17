from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

import httpx
from pydantic_ai import Agent, CallDeferred, RunContext
from pydantic_ai.common_tools.web_fetch import web_fetch_tool
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness import CodeMode

from octomate.config import ModelConfig, PulseAgentConfig
from octomate.schemas.actions import AgentMessage
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.agent.context import RetryTransport, SessionContext
from octomate.tentacles.agent.pulse.prompts import STEP_PROMPT, SYSTEM_PROMPT
from octomate.tentacles.agent.pulse.run import streaming
from octomate.tentacles.agent.pulse.state import (
    LocalSubAgent,
    PulseOutput,
    PulseState,
    SubAgent,
)
from octomate.tentacles.agent.pulse.tools import (
    build_bash_tool,
    build_delegate_toolset,
    build_file_management_toolset,
    build_todo_list_toolset,
)
from octomate.tentacles.agent.skills import SkillManager

if TYPE_CHECKING:
    from octomate.octopus import Octopus

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
    """Octomate's pulse — model-loop with tool-driven planning via todo_list.

    Channels invoke process() directly (not via beckon) to run the pulse loop.
    Can also be beckoned as an AgentTentacle via run() for silent plan steps.
    """

    pulse_agent: Agent[SessionContext, PulseOutput]
    subagents: dict[str, SubAgent]
    toolsets: Sequence[AbstractToolset[SessionContext]] | None
    subagent_catalog: str | None
    max_turns: int

    def __init__(
        self,
        tag: str,
        octopus: Octopus,
        config: PulseAgentConfig,
        skill_manager: SkillManager | None = None,
    ) -> None:
        super().__init__(
            tag,
            octopus,
            description=config.description,
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

        skill_toolsets = skill_manager.build_toolsets() if skill_manager else []
        summon_toolset = build_summon_toolset(octopus.agent_tentacles)
        self.toolsets = [
            *skill_toolsets,
            build_file_management_toolset(),
            *([summon_toolset] if summon_toolset else []),
        ] or None

        self.pulse_agent = Agent(
            main_model,
            system_prompt=SYSTEM_PROMPT,
            deps_type=SessionContext,
            output_type=[list[AgentMessage], DeferredToolRequests],
            tools=[web_fetch_tool(), build_bash_tool()],
            toolsets=self.toolsets,
            model_settings=main_settings,
        )

        self.max_turns = config.max_turns

        self.subagents = {}
        for sub_tag, sub_cfg in config.subagents.items():
            sub_model = _build_model(sub_cfg, http_client)
            sub_settings = (
                ModelSettings(thinking=sub_cfg.thinking) if sub_cfg.thinking else None
            )
            self.subagents[sub_tag] = LocalSubAgent(
                id=sub_tag,
                description=sub_cfg.description or f"Pulse subagent {sub_tag}",
                agent=Agent(
                    sub_model,
                    system_prompt=sub_cfg.system_prompt or STEP_PROMPT,
                    deps_type=SessionContext,
                    output_type=str,
                    capabilities=[CodeMode()] if sub_cfg.code_mode else [],
                    model_settings=sub_settings,
                ),
            )

        if self.subagents:
            lines = [
                f'- "{sub_tag}": {sub.description}'
                for sub_tag, sub in self.subagents.items()
            ]
            self.subagent_catalog = (
                "[available step assignees (subagents)]\n" + "\n".join(lines)
            )
        else:
            self.subagent_catalog = None

    async def run(
        self,
        key: Any,
        contents: list[Any],
        *,
        session_name: str = "",
        silent: bool = False,
    ) -> str | None:
        channel = self.octopus.tentacles.get(key.tentacle_id)
        if channel is None:
            return None

        context = f"[group: {key.group_id}]" if key.group_id else "[chat: private]"
        header = f"[me: {channel.name} ({channel.profile.user_id})] {context}"
        user_prompt: list = [header]
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
        instructions = (
            "\n\n".join(filter(None, [self.subagent_catalog, memory_instructions]))
            or None
        )
        run_toolsets: list[AbstractToolset[SessionContext]] = [
            *list(channel.toolsets),
            build_todo_list_toolset(state),
            *(
                [delegate]
                if (delegate := build_delegate_toolset(self.subagents, state))
                else []
            ),
        ]

        stream_ctx = contextlib.nullcontext() if silent else channel.open_stream(key)
        try:
            async with stream_ctx as stream:
                result = await streaming(
                    self.pulse_agent,
                    stream,
                    tentacle=channel,
                    user_prompt=state.prompt,
                    deps=session_ctx,
                    toolsets=run_toolsets or None,
                    instructions=instructions,
                    message_history=message_history,
                    usage_limits=UsageLimits(request_limit=self.max_turns),
                )
                messages = cast(list[AgentMessage], result.output)
        except UsageLimitExceeded:
            logger.warning("[%s] Pulse hit max_turns limit (%d)", key, self.max_turns)
            messages = [
                AgentMessage(
                    segments=[
                        TextSegment(
                            data={"text": "Sorry, I hit my turn limit on this task."}
                        )
                    ]
                )
            ]
        except Exception:
            logger.exception("[%s] Pulse encountered an error", key)
            messages = [
                AgentMessage(
                    segments=[
                        TextSegment(
                            data={
                                "text": "Something went wrong while processing your request."
                            }
                        )
                    ]
                )
            ]
        finally:
            if state.card_ref:
                await channel.feelers.todos.unpin_todo(key, state.card_ref)

        if not silent:
            for msg in messages:
                await channel.twitch(key, msg.segments)
            asyncio.create_task(channel.memory.memo(key, messages, channel))

        return "\n".join(str(seg) for msg in messages for seg in msg.segments)
