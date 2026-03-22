from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.tools import DeferredToolRequests

from octomate.agents.manager import SkillManager
from octomate.agents.prompts import BASE_PROMPT
from octomate.config import SurgeConfig
from octomate.schemas.actions import AgentMessage
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.schemas.session import SessionKey
from octomate.tentacles.base import SendTarget

if TYPE_CHECKING:
    from octomate.tentacles.base import Tentacle

SYSTEM_PROMPT = BASE_PROMPT


@dataclass
class SessionContext:
    session_key: SessionKey
    active_skills: set[str] = field(default_factory=set)
    tentacle: Tentacle | None = None
    event: MessageEvent | None = None
    handed_over: bool = False


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3


class RetryTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        last_response: httpx.Response | None = None
        for attempt in range(MAX_RETRIES + 1):
            response = await self._transport.handle_async_request(request)
            if response.status_code not in RETRYABLE_STATUS_CODES:
                return response
            last_response = response
            if attempt < MAX_RETRIES:
                retry_after = response.headers.get("retry-after")
                if retry_after and retry_after.isdigit():
                    delay = min(float(retry_after), 60.0)
                else:
                    delay = 2**attempt
                await asyncio.sleep(delay)
        return last_response  # type: ignore[return-value]


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

    @agent.tool
    async def acknowledge(ctx: RunContext[SessionContext], text: str) -> str:
        """Send a short message to the user immediately before doing heavy work.

        Call this FIRST when about to invoke a skill or tool that may take a few
        seconds (e.g. weather, search, knowledge base), so the user knows you are
        working on it. Do NOT use for simple replies like greetings or responses
        that don't involve tool calls. Example: acknowledge("let me look that up~")
        """
        if ctx.deps.tentacle:
            key = ctx.deps.session_key
            if key.group_id is not None:
                target = SendTarget("group", key.group_id)
            else:
                target = SendTarget("private", key.user_id)
            await ctx.deps.tentacle.twitch(target, [TextSegment(data={"text": text})])
        return "acknowledged"

    @agent.tool
    async def ask_user(
        ctx: RunContext[SessionContext],
        question: str,
        options: list[str] | None = None,
    ) -> str:
        """Ask the user a question and wait for their answer before continuing.

        Use this when you need clarification or a decision from the user. Do NOT
        send a separate text message asking the same thing — this tool handles it.
        Provide options to show choice buttons; omit for free-text input
        (platform-dependent — may not be supported everywhere).
        Returns the user's answer, or '(no response)' on timeout.
        """
        if not ctx.deps.tentacle:
            return "(no response)"
        key = ctx.deps.session_key
        target = (
            SendTarget("group", key.group_id)
            if key.group_id
            else SendTarget("private", key.user_id)
        )
        resp = await ctx.deps.tentacle.feelers.questions.ask_question(
            target, question, options
        )
        return resp.answer if resp else "(no response)"

    @agent.tool
    async def create_todo(ctx: RunContext[SessionContext], title: str) -> str:
        """Create a TODO card for the user in the current chat.

        Use this whenever a task has multiple stages or steps — create a todo item
        for each stage so the user can track progress. Returns a todo ID on success,
        or an error message if not supported on this platform.
        """
        if not ctx.deps.tentacle:
            return "not supported"
        key = ctx.deps.session_key
        target = (
            SendTarget("group", key.group_id)
            if key.group_id
            else SendTarget("private", key.user_id)
        )
        item = await ctx.deps.tentacle.feelers.todos.create_todo(target, title)
        return f"todo:{item.todo_id}" if item else "not supported on this platform"

    return agent
