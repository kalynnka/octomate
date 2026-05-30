from __future__ import annotations

from typing import Protocol

from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults


class DeferredResolver(Protocol):
    async def resolve(self, requests: DeferredToolRequests) -> DeferredToolResults: ...
