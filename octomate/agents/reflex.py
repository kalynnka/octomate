from __future__ import annotations

import httpx
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.tools import DeferredToolRequests

from octomate.agents.mind import RetryTransport, SessionContext
from octomate.agents.prompts import BASE_PROMPT, REFLEX_EXTRA
from octomate.config import ReflexConfig
from octomate.schemas.actions import AgentMessage
from octomate.schemas.segments import TextSegment
from octomate.tentacles.base import SendTarget

SYSTEM_PROMPT = BASE_PROMPT + REFLEX_EXTRA


def create_reflex_agent(
    config: ReflexConfig,
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

    agent: Agent[SessionContext, list[AgentMessage] | DeferredToolRequests] = Agent(
        model,
        system_prompt=SYSTEM_PROMPT,
        deps_type=SessionContext,
        output_type=[list[AgentMessage], DeferredToolRequests],
    )

    @agent.tool
    async def acknowledge(ctx: RunContext[SessionContext], text: str) -> str:
        """Send a short message to the user immediately before doing work.

        Call this when about to use the handover tool so the user knows you're
        on it. Example: acknowledge("on it!") then call handover(...).
        """
        if ctx.deps.tentacle:
            key = ctx.deps.session_key
            target = (
                SendTarget("group", key.group_id)
                if key.group_id
                else SendTarget("private", key.user_id)
            )
            await ctx.deps.tentacle.twitch(target, [TextSegment(data={"text": text})])
        return "acknowledged"

    @agent.tool
    async def handover(ctx: RunContext[SessionContext], hint: str) -> str:
        """Escalate this conversation to the main brain for deep processing.

        Use when the request needs tools, skills, coding, research, or complex
        reasoning that you cannot handle directly. Set hint to a brief
        description of what the brain should focus on (e.g. "user wants to
        query GitHub PRs", "needs weather API", "multi-step planning task").
        The brain will process the original message with full tool access.
        """
        tentacle = ctx.deps.tentacle
        event = ctx.deps.event
        if not tentacle or not event:
            return "handover failed: no tentacle or event"
        octopus = tentacle.octopus
        await octopus.think(event.session_key, [event], hint=hint)
        return "handed over to brain"

    return agent
