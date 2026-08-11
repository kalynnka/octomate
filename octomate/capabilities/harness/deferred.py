"""Deferred-tool resolution protocols — harness interfaces for the react loop.

Two complementary hooks (keep both):

- `DeferredResolver`: resolve deferred calls in-process and let the loop continue.
- `DeferredSuspender`: persist + present the requests out-of-band; the run ends and
  is resumed later by feeding the collected results back through a fresh run.

The concrete, channel-coupled suspender (`HumanReviewSuspender`) lives in the
triage layer (`octomate.reflex.suspender`) so the harness never imports
channels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic_ai import ToolDenied
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from octomate.capabilities.harness.events import ActionBatchEvent


class DeferredResolver(Protocol):
    """Resolve deferred tool calls in-process so the react loop can continue."""

    async def resolve(self, requests: DeferredToolRequests) -> DeferredToolResults: ...


@dataclass
class DeclineResolver:
    """Resolves every deferred action by declining it immediately — the
    resolver for a non-interactive run (e.g. a commissioned accomplice). There is no
    human to ask, so approvals are denied and asks are answered with the
    decline, and the react loop continues to a final answer in-process instead
    of parking anything for review."""

    message: str = (
        "Declined: this run has no user to ask. Proceed on your best judgment "
        "and state the assumption in your report."
    )

    async def resolve(self, requests: DeferredToolRequests) -> DeferredToolResults:
        results = DeferredToolResults()
        for approval in requests.approvals:
            results.approvals[approval.tool_call_id] = ToolDenied(self.message)
        for call in requests.calls:
            results.calls[call.tool_call_id] = self.message
        return results


@dataclass
class ApproveResolver:
    """Resolves every deferred approval by granting it, and asks by declining — the
    resolver for a `bypassPermissions` conversation.

    Approvals are what the posture speaks about, so they are granted without a card. An
    ask is a question only a human can answer, and bypassing gating says nothing about
    knowing the answer, so those still decline and the run reports the assumption.
    """

    message: str = (
        "Declined: this conversation bypasses approvals but still cannot ask you. "
        "Proceed on your best judgment and state the assumption in your report."
    )

    async def resolve(self, requests: DeferredToolRequests) -> DeferredToolResults:
        results = DeferredToolResults()
        for approval in requests.approvals:
            results.approvals[approval.tool_call_id] = True
        for call in requests.calls:
            results.calls[call.tool_call_id] = self.message
        return results


class DeferredSuspender(Protocol):
    """Human-in-the-loop hook for deferred tool calls the agent cannot resolve
    in process. Unlike `DeferredResolver`, which returns results and lets the
    react loop continue, a suspender persists + presents the requests for an
    out-of-band response and the run ends; it is resumed later by feeding the
    collected `DeferredToolResults` back through a fresh agent run.

    Returning an `ActionBatchEvent` asks the react loop to present the batch *on
    the stream* (the consumer renders it); returning `None` means the suspender
    presented it out-of-band itself.
    """

    async def suspend(
        self, requests: DeferredToolRequests
    ) -> ActionBatchEvent | None: ...
