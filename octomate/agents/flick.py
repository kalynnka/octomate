from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from pydantic_ai import Agent, CallDeferred, RunContext
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import FunctionToolset

from octomate.agents.base import RetryTransport, SessionContext
from octomate.agents.manager import SkillManager
from octomate.agents.prompts import BASE_PROMPT, FLICK_EXTRA
from octomate.config import FlickConfig
from octomate.schemas.actions import AgentMessage

if TYPE_CHECKING:
    from octomate.tentacles.base import AgentTentacle

SYSTEM_PROMPT = BASE_PROMPT + FLICK_EXTRA


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


def create_flick_agent(
    config: FlickConfig,
    skill_manager: SkillManager | None = None,
    summon_toolset: FunctionToolset[SessionContext] | None = None,
) -> Agent[SessionContext, list[AgentMessage] | DeferredToolRequests]:
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

    toolsets = skill_manager.build_skillsets() if skill_manager else []
    if summon_toolset:
        toolsets.append(summon_toolset)

    return Agent(
        model,
        system_prompt=SYSTEM_PROMPT,
        deps_type=SessionContext,
        output_type=[list[AgentMessage], DeferredToolRequests],
        toolsets=toolsets or None,
    )
