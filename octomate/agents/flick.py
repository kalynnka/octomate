from __future__ import annotations

import httpx
from pydantic_ai import Agent
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.tools import DeferredToolRequests

from octomate.agents.base import RetryTransport, SessionContext
from octomate.agents.manager import SkillManager
from octomate.agents.prompts import BASE_PROMPT, FLICK_EXTRA
from octomate.config import FlickConfig
from octomate.schemas.actions import AgentMessage

SYSTEM_PROMPT = BASE_PROMPT + FLICK_EXTRA
SYSTEM_PROMPT = BASE_PROMPT


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

    # @agent.tool(requires_approval=True)
    # async def handover(ctx: RunContext[SessionContext], summary: str) -> str:
    #     """Escalate this conversation to surge for deep processing.

    #     Use when the request want to do coding, research, or complex
    #     reasoning tasks. Write a clear summary that
    #     captures the user's actual request and any relevant context from the
    #     conversation. Surge will see only this summary plus recalled memories —
    #     not the raw chat history.
    #     """
    #     tentacle = ctx.deps.tentacle
    #     event = ctx.deps.event
    #     if not tentacle or not event:
    #         return "handover failed: no tentacle or event"
    #     octopus = tentacle.octopus
    #     asyncio.create_task(octopus.surge(event.session_key, summary=summary))
    #     ctx.deps.handed_over = True
    #     return "handed over to surge"

    return agent
