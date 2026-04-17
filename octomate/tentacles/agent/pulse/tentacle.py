from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

import httpx
from pydantic_ai import Agent, CallDeferred, RunContext
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import RetryPromptPart, ToolCallPart
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
from octomate.tentacles.agent.pulse.prompts import STEP_PROMPT, build_system_prompt
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
    build_web_fetch_tool,
    build_web_search_tool,
)
from octomate.tentacles.agent.skills import SkillManager

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

    from octomate.octopus import Octopus
    from octomate.tentacles.agent.skills.distiller import SkillDistiller

logger = logging.getLogger(__name__)


def _populate_state_counters(state: PulseState, messages: list[ModelMessage]) -> None:
    for msg in messages:
        for part in msg.parts:
            if isinstance(part, ToolCallPart):
                state.tool_call_count += 1
                state.distinct_tools_used.add(part.tool_name)
                if part.tool_name == "load_skill":
                    args = part.args_as_dict() if callable(part.args_as_dict) else {}
                    skill_name = args.get("name", "")
                    if skill_name:
                        state.loaded_skills.add(skill_name)
            elif isinstance(part, RetryPromptPart):
                state.error_count += 1


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
    skill_manager: SkillManager | None

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
            timeout=httpx.Timeout(None, connect=10.0),
        )

        main_model = _build_model(config.main, http_client)
        main_settings = (
            ModelSettings(thinking=config.main.thinking)
            if config.main.thinking
            else None
        )

        skill_toolsets = skill_manager.build_toolsets() if skill_manager else []
        self.toolsets = [
            *skill_toolsets,
            build_file_management_toolset(),
        ] or None

        self.pulse_agent = Agent(
            main_model,
            system_prompt=build_system_prompt(config.skill.evolving),
            deps_type=SessionContext,
            output_type=[list[AgentMessage], DeferredToolRequests],
            tools=[
                build_web_fetch_tool(config.web_tool.jina_reader),
                build_web_search_tool(config.web_tool.querit),
                build_bash_tool(),
            ],
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

        self.skill_manager = skill_manager
        self._distiller: SkillDistiller | None = None

    async def __aenter__(self) -> PulseTentacle:
        if self.skill_manager:
            await self.skill_manager.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self.skill_manager:
            await self.skill_manager.__aexit__(*args)

    @property
    def distiller(self) -> SkillDistiller | None:
        return self._distiller

    @distiller.setter
    def distiller(self, value: SkillDistiller | None) -> None:
        self._distiller = value

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
        summon_toolset = build_summon_toolset(self.octopus.agent_tentacles)
        run_toolsets: list[AbstractToolset[SessionContext]] = [
            *list(channel.toolsets),
            build_todo_list_toolset(state),
            *(
                [delegate]
                if (delegate := build_delegate_toolset(self.subagents, state))
                else []
            ),
            *([summon_toolset] if summon_toolset else []),
        ]

        stream_ctx = contextlib.nullcontext() if silent else channel.open_stream(key)
        all_model_messages: list[Any] = []
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
                all_model_messages = result.all_messages()
                _populate_state_counters(state, all_model_messages)
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
            if self.distiller is not None and self.distiller.should_distill(state):
                t = asyncio.create_task(
                    self.distiller.distill(all_model_messages, state.goal, state),
                    name="skill-distill",
                )
                t.add_done_callback(
                    lambda fut: fut.exception() if not fut.cancelled() else None
                )

        return "\n".join(str(seg) for msg in messages for seg in msg.segments)
