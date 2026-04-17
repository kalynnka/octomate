from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from octomate.schemas.events import MessageEvent
from octomate.schemas.session import SessionKey

if TYPE_CHECKING:
    from octomate.tentacles.channel.base import ChannelTentacle

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 6


@dataclass
class SummonRequest:
    tentacle_tag: str
    summary: str
    user_prefer: str = ""
    language: str = ""
    name: str = ""


@dataclass
class SessionContext:
    session_key: SessionKey
    tentacle: ChannelTentacle
    event: MessageEvent | None = None


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
                logger.warning(
                    "Retryable %d from %s %s, attempt %d/%d, retrying in %.1fs",
                    response.status_code,
                    request.method,
                    request.url,
                    attempt + 1,
                    MAX_RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)
        return last_response  # type: ignore[return-value]
