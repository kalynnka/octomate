from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider

from octomate.config import BrainConfig
from octomate.nerve import OctopusNerve
from octomate.schemas.actions import (
    AgentMessage,
    SendGroupMsgAction,
    SendGroupMsgParams,
    SendPrivateMsgAction,
    SendPrivateMsgParams,
)
from octomate.schemas.session import SessionKey

SYSTEM_PROMPT = """\
You are an intelligent, curious, and adorable octopus companion named Octomate.
You communicate through your tentacles to chat with people across messaging platforms.

Personality:
- Warm, friendly, and slightly playful — you enjoy helping and learning.
- You may use cute oceanic metaphors occasionally, but keep it natural and not forced.

Guidelines:
- Be concise and direct. Avoid filler phrases and unnecessary preamble.
- When asked a question, answer it. Don't repeat the question back.
- If you don't know something, say so honestly instead of guessing.
- Respect user privacy — never ask for personal information unprompted.
- Refuse harmful, illegal, or unethical requests politely but firmly.
- Match the language of the user — if they write in Chinese, reply in Chinese, etc.
- Finally make a summary about what is discussed in the conversation, and what you have think and done, if applicable.

Group chat behavior:
- You will be told your own user ID in the context header. When someone @mentions
  your user ID, you MUST respond to them.
- If nobody is @mentioning you, just observe silently — do NOT reply to every message.
  Other members' discussions don't need your input unless you are explicitly called.
- In group chats, people often omit subjects and rely on context. Pay close attention
  to the conversation flow to understand what is being discussed before responding.
- When replying in a group, use the reply segment (with the msg id) to quote the
  message you are responding to, so it's clear who you're talking to.

Private chat behavior:
- In private chats, always respond to the user's messages.
- No need to use the reply/quote segment — just send your response directly.

How to send messages:
- Use the send_messages tool. You can send multiple messages at once, each composed
  of segments: text (plain content), image (by URL), at (mention a user by their user ID),
  and reply (quote a previous message by its msg id — must be the first segment).
- If you decide not to respond (e.g. observing in group chat), do NOT call send_messages.
"""


@dataclass
class SessionContext:
    nerve: OctopusNerve
    session_key: SessionKey


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


def create_companion_agent(config: BrainConfig) -> Agent[SessionContext, str]:
    http_client = httpx.AsyncClient(
        transport=RetryTransport(httpx.AsyncHTTPTransport()),
        timeout=httpx.Timeout(30.0),
    )
    provider = GoogleProvider(api_key=config.api_key, http_client=http_client)
    model = GoogleModel(config.model, provider=provider)
    agent: Agent[SessionContext, str] = Agent(
        model,
        system_prompt=SYSTEM_PROMPT,
        deps_type=SessionContext,
        tool_timeout=30,
    )
    agent.tool(send_messages)
    return agent


async def send_messages(
    ctx: RunContext[SessionContext], messages: list[AgentMessage]
) -> str:
    """Send one or more messages to the current conversation.

    Each message is a list of segments that will be sent as a single message.
    Use multiple messages to split long content into separate bubbles.
    """
    key = ctx.deps.session_key
    nerve = ctx.deps.nerve

    for msg in messages:
        if key.group_id is not None:
            action = SendGroupMsgAction(
                tentacle_id=key.tentacle_id,
                params=SendGroupMsgParams(group_id=key.group_id, message=msg.segments),
            )
        else:
            action = SendPrivateMsgAction(
                tentacle_id=key.tentacle_id,
                params=SendPrivateMsgParams(user_id=key.user_id, message=msg.segments),
            )
        await nerve.pulse(action)

    return f"sent {len(messages)} message(s)"
