"""Deferred-tool resolution protocols — harness interfaces for the react loop.

Two complementary hooks (keep both):

- `DeferredResolver`: resolve deferred calls in-process and let the loop continue.
- `DeferredSuspender`: persist + present the requests out-of-band; the run ends and
  is resumed later by feeding the collected results back through a fresh run.

The concrete, channel-coupled suspender (`HumanReviewSuspender`) lives in the
domain layer (`tentacles.agent.graph.suspender`) so the harness never imports
channels.
"""

from __future__ import annotations

from typing import Protocol

from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from octomate.capabilities.events import ActionBatchEvent


class DeferredResolver(Protocol):
    """Resolve deferred tool calls in-process so the react loop can continue."""

    async def resolve(self, requests: DeferredToolRequests) -> DeferredToolResults: ...


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
