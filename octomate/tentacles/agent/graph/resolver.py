from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Protocol

from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults


class DeferredResolver(Protocol):
    async def resolve(self, requests: DeferredToolRequests) -> DeferredToolResults: ...


@dataclass
class StubResolver:
    DEFAULT_RESPONSE: ClassVar[str] = "(stub) handled {tool_name}"

    approve_all: bool = True
    canned: dict[str, str] = field(default_factory=dict)

    async def resolve(self, requests: DeferredToolRequests) -> DeferredToolResults:
        result = DeferredToolResults()
        for call in requests.calls:
            result.calls[call.tool_call_id] = self.canned.get(
                call.tool_name,
                self.DEFAULT_RESPONSE.format(tool_name=call.tool_name),
            )
        for call in requests.approvals:
            result.approvals[call.tool_call_id] = self.approve_all
        return result
