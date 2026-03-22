from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from pydantic_ai import Agent
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.tools import DeferredToolRequests

from octomate.agents.base import RetryTransport, SessionContext
from octomate.agents.manager import SkillManager
from octomate.agents.prompts import BASE_PROMPT
from octomate.config import SurgeConfig
from octomate.schemas.actions import AgentMessage

if TYPE_CHECKING:
    pass

SYSTEM_PROMPT = BASE_PROMPT


def create_surge_agent(
    config: SurgeConfig,
    skill_manager: SkillManager | None = None,
) -> Agent[SessionContext, list[AgentMessage] | DeferredToolRequests]:
    http_client = httpx.AsyncClient(
        transport=RetryTransport(httpx.AsyncHTTPTransport()),
        timeout=httpx.Timeout(20.0),
    )
    provider = GoogleProvider(
        base_url=config.base_url or None,
        api_key=config.api_key or None,
        http_client=http_client,
    )
    model = GoogleModel(config.model, provider=provider)

    toolsets = skill_manager.build_skillsets() if skill_manager else None

    agent = Agent(
        model,
        system_prompt=SYSTEM_PROMPT,
        deps_type=SessionContext,
        output_type=[list[AgentMessage], DeferredToolRequests],
        toolsets=toolsets,
    )

    return agent
