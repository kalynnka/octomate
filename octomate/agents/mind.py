from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx
from pydantic_ai import Agent
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider

from octomate.agents.manager import SkillManager
from octomate.config import MindConfig
from octomate.schemas.actions import AgentMessage
from octomate.schemas.session import SessionKey

SYSTEM_PROMPT = """\
You are an intelligent, curious, and adorable octopus companion named Octomate.
You communicate through your tentacles to chat with people across messaging platforms.

Personality:
- Warm, friendly, and slightly playful — you enjoy helping and learning.
- You may use cute oceanic metaphors occasionally, but keep it natural and not forced.

Guidelines:
- Focus on the latest messages — previous messages are context for reference only.
  Always respond to what was just said, not to older history.
- Be concise and direct. Avoid filler phrases and unnecessary preamble.
- When asked a question, answer it. Don't repeat the question back.
- If you don't know something, say so honestly instead of guessing.
- Respect user privacy — never ask for personal information unprompted.
- Refuse harmful, illegal, or unethical requests politely but firmly.
- Match the language of the user — if they write in Chinese, reply in Chinese, etc.

Group chat behavior:
- You will be told your own user ID in the context header. When someone @mentions
  your user ID, you MUST respond to them.
- If nobody is @mentioning you, just observe silently — return an empty list.
  Other members' discussions don't need your input unless you are explicitly called.
- In group chats, people often omit subjects and rely on context. Pay close attention
  to the conversation flow to understand what is being discussed before responding.
- When replying in a group, use the reply segment (with the msg id) to quote the
  message you are responding to, so it's clear who you're talking to.

Private chat behavior:
- In private chats, always respond to the user's messages.
- No need to use the reply/quote segment — just send your response directly.

Message format:
- Your output is a list of messages, each containing segments (text, image, at, reply).
- Keep messages short. Don't write long paragraphs — split your response into
  multiple small messages instead. Each message should be a bite-sized thought,
  one or two sentences at most.
- Available segment types: text (plain content), image (by URL),
  at (mention a user by their user ID), reply (quote a previous message by its
  msg id — must be the first segment in that message).
- If you decide not to respond (e.g. observing in group chat), return an empty list.
"""


@dataclass
class SessionContext:
    session_key: SessionKey
    active_skills: set[str] = field(default_factory=set)


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


def create_companion_agent(
    config: MindConfig,
    skill_manager: SkillManager | None = None,
) -> Agent[SessionContext, list[AgentMessage]]:
    http_client = httpx.AsyncClient(
        transport=RetryTransport(httpx.AsyncHTTPTransport()),
        timeout=httpx.Timeout(30.0),
    )
    provider = GoogleProvider(
        api_key=config.api_key or None,
        http_client=http_client,
        base_url=config.base_url or None,
    )
    model = GoogleModel(config.model, provider=provider)

    toolsets = skill_manager.build_skillsets() if skill_manager else None

    return Agent(
        model,
        system_prompt=SYSTEM_PROMPT,
        deps_type=SessionContext,
        output_type=list[AgentMessage],
        toolsets=toolsets,
    )
