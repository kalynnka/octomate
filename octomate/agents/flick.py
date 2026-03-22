from __future__ import annotations

import httpx
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.tools import DeferredToolRequests

from octomate.agents.base import RetryTransport, SessionContext
from octomate.agents.manager import SkillManager
from octomate.agents.prompts import BASE_PROMPT, FLICK_EXTRA
from octomate.config import FlickConfig
from octomate.schemas.actions import AgentMessage

SYSTEM_PROMPT = BASE_PROMPT + FLICK_EXTRA


def create_flick_agent(
    config: FlickConfig,
    skill_manager: SkillManager | None = None,
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

    toolsets = skill_manager.build_skillsets() if skill_manager else None

    agent: Agent[SessionContext, list[AgentMessage] | DeferredToolRequests] = Agent(
        model,
        system_prompt=SYSTEM_PROMPT,
        deps_type=SessionContext,
        output_type=[list[AgentMessage], DeferredToolRequests],
        toolsets=toolsets,
    )

    @agent.tool(requires_approval=True)
    async def summon(
        ctx: RunContext[SessionContext],
        summary: str,
        user_prefer: str,
        language: str,
    ) -> str:
        """Summon surge for deep processing.

        Use when user explicitly requests, or requires coding, research, or complex reasoning.
        Write a clear summary capturing the user's actual request and relevant context.
        Surge only sees this summary — not the raw chat history.

        Args:
            summary: A clear summary of the user's request and relevant context for surge.
            user_prefer: Any explicit user preferences or constraints mentioned (e.g. "use Python", "keep it short").
            language: The language the user is communicating in (e.g. "en", "zh", "ja").
        """
        ctx.deps.summon.active = True
        ctx.deps.summon.summary = summary
        ctx.deps.summon.user_prefer = user_prefer
        ctx.deps.summon.language = language
        return "summoned surge"

    return agent
