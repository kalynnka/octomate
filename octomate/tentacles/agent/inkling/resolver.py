from dataclasses import dataclass, field
from typing import ClassVar, Protocol

from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults


class DeferredResolver(Protocol):
    """Resolves deferred tool calls (CallDeferred + requires_approval) into results."""

    async def resolve(self, requests: DeferredToolRequests) -> DeferredToolResults: ...


@dataclass
class StubResolver:
    """Auto-approves all approvals and returns canned strings for all CallDeferred calls.

    Used for smoke tests and as a placeholder until real HITL bridges (channel feelers,
    confirmations, summon dispatch) are wired in.
    """

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
