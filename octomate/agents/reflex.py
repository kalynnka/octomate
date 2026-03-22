from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import httpx
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider

from octomate.agents.mind import RetryTransport, SessionContext
from octomate.config import ReflexConfig
from octomate.schemas.actions import AgentMessage
from octomate.schemas.segments import TextSegment
from octomate.tentacles.base import SendTarget

if TYPE_CHECKING:
    pass

REFLEX_SYSTEM_PROMPT = """\
You are the reflex layer of an IM bot called Octomate — a fast, confident triage agent.
You see each incoming message first and decide how to handle it.

Triage decisions:

ANSWER — handle it yourself:
- Greetings, thanks, casual small talk
- Simple factual questions you can answer confidently without any external data
- Short opinions, encouragement, humor
- Anything a smart, knowledgeable person could answer off the top of their head

HANDOVER — escalate to the main brain:
- Anything requiring tools, skills, or external APIs (GitHub, search, weather, code execution…)
- Complex reasoning, analysis, summarization of documents
- Multi-step tasks, planning, anything the user expects real effort on
- Requests where being wrong would be worse than asking the brain
- When in doubt: handover. The brain is powerful; use it.
Set a brief `reason` hint so the brain knows what to prepare for.

SILENT — stay quiet (do NOT set messages):
- Group chat messages where the bot is NOT @mentioned
- Spam, noise, messages clearly not addressed to the bot

Language: always reply in the same language the user wrote in.
Style: warm, direct, brief — one or two sentences max when answering.
"""


class ReflexDecision(StrEnum):
    ANSWER = "answer"
    HANDOVER = "handover"
    SILENT = "silent"


@dataclass
class ReflexResult:
    decision: ReflexDecision
    messages: list[AgentMessage] = field(default_factory=list)
    reason: str = ""


def create_reflex_agent(
    config: ReflexConfig,
) -> Agent[SessionContext, ReflexResult]:
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

    agent: Agent[SessionContext, ReflexResult] = Agent(
        model,
        system_prompt=REFLEX_SYSTEM_PROMPT,
        deps_type=SessionContext,
        output_type=ReflexResult,
    )

    @agent.tool
    async def acknowledge(ctx: RunContext[SessionContext], text: str) -> str:
        """Send a short message to the user immediately — use before HANDOVER to
        confirm you received the request while the main brain processes it.
        Example: acknowledge("on it!") then set decision=HANDOVER.
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

    return agent
