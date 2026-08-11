"""Deferred-tool resolution protocols — harness interfaces for the react loop.

Two complementary hooks (keep both):

- `DeferredResolver`: resolve deferred calls in-process and let the loop continue.
- `DeferredSuspender`: persist + present the requests out-of-band; the run ends and
  is resumed later by feeding the collected results back through a fresh run.

Which of the two a deferral gets is `ResolverChoice`'s answer, and the loop asks it
anew every time round rather than once per run.

The concrete, channel-coupled suspender (`HumanReviewSuspender`) lives in the
triage layer (`octomate.reflex.suspender`) so the harness never imports
channels.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic_ai import ToolDenied
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from octomate.capabilities.ask import ASK_DEFERR_KIND
from octomate.capabilities.harness.events import ActionBatchEvent
from octomate.schemas.conversation import Conversation


class DeferredResolver(Protocol):
    """Resolve deferred tool calls in-process so the react loop can continue.

    A resolver answers what it speaks for and leaves the rest alone — a posture has
    an opinion about approvals and questions, and none at all about a `teleport`. It
    is not the resolver's job to notice that it covered only part of the batch:
    `ResolveDeferred` is where that is noticed, once, for whatever ran. The runner
    refuses results covering only some of the calls it deferred, so a batch nobody
    wholly answered goes to the suspender entire.
    """

    async def resolve(self, requests: DeferredToolRequests) -> DeferredToolResults: ...


class ResolverChoice(Protocol):
    """Choose the resolvers for one deferral, from the conversation as it stands at
    that moment.

    Asked at every deferral rather than once per run, because what may be resolved
    without a human is the conversation's approval posture — and that is switched
    while runs are going. Composed once, a switch would only reach the run after the
    one it was made during, which is the turn nobody was watching.

    Several, in order, because one batch can hold deferrals of different kinds and no
    single policy speaks for all of them: each resolver answers its own and earlier
    ones win, so a catch-all goes last. An empty sequence means nothing answers this
    batch in-process — it goes to the suspender, and back to the caller when there is
    none.
    """

    # Positional-only, so an implementation is free to name the conversation
    # whatever reads best — a callable protocol otherwise pins the parameter's name.
    def __call__(self, conversation: Conversation, /) -> Sequence[DeferredResolver]: ...


@dataclass
class DeclineResolver:
    """Resolves every deferred action by declining it immediately — the catch-all
    that closes a non-interactive run's chain (e.g. a commissioned accomplice). There
    is no human to ask, so approvals are denied and asks are answered with the
    decline, and the react loop continues to a final answer in-process instead
    of parking anything for review.

    Last in the chain and answering everything, which is what guarantees a batch is
    covered: a run with no human has no suspender either, so anything left open would
    end the run holding requests nobody is coming to answer. Whatever ran before it
    has already had its say — earlier answers stand."""

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


@dataclass(frozen=True)
class PostureResolver:
    """Answers the two things an approval posture speaks about, and nothing else:
    the approvals it gates, and the questions it says not to put to a human.

    What it walks past is the point. A `teleport` is deferred the same way a question
    is, but it is neither an approval nor a question — no posture has an opinion on
    it, and the suspender is what performs one. Left unanswered here, the batch is
    not wholly covered and all of it goes to the human instead. A deferral arriving
    with a kind this does not know goes the same way, so an unrecognized one reaches
    a human rather than being answered on their behalf.
    """

    # Whether an approval is granted outright, or denied with `message` as the reason.
    approve: bool
    # What the agent is told in place of the answer it will not be given — a denial's
    # reason, and an unasked question's return.
    message: str

    async def resolve(self, requests: DeferredToolRequests) -> DeferredToolResults:
        results = DeferredToolResults()
        for call in requests.calls:
            if (
                requests.metadata.get(call.tool_call_id, {}).get("kind")
                == ASK_DEFERR_KIND
            ):
                results.calls[call.tool_call_id] = self.message
        for approval in requests.approvals:
            results.approvals[approval.tool_call_id] = (
                True if self.approve else ToolDenied(self.message)
            )
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
