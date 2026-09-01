from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from pydantic_ai.tools import DeferredToolRequests

from octomate.capabilities.harness.events import ActionBatchEvent
from octomate.managers.conversation import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import (
    BIND_DEFER_KIND,
    TELEPORT_DEFER_KIND,
    CrossingLanding,
    ResponseTargetMode,
    RunName,
    SummonDecision,
)
from octomate.telemetry import reflex_logfire
from octomate.tentacles.channels.base import ChannelTentacle


@dataclass(frozen=True)
class TeleportRequest:
    """A teleport for the graph to perform: fork the history and resume the agent
    in the new place. Reached two ways — an Inkling run defers the `teleport` call
    mid-run (classified out of its `DeferredToolRequests` by metadata kind), while
    a runtime that cannot be suspended records a `TeleportDecision` its turn's end
    converts into one of these."""

    hint: str
    # The deferred call to resolve into the resumed run. None for a
    # decision-reported teleport, which has no pending call — the resumed run
    # opens from the hint instead.
    tool_call_id: str | None = None
    # Where it goes, when that is not a sub-thread of the chat it is already in. The
    # gateway refused a channel this agent does not run and one that opens no
    # sub-thread, so the node has a place to open and no fallback to choose.
    crossing: CrossingLanding | None = None


@dataclass(frozen=True)
class BindRequest:
    """A bind for the graph to resume from: the run that cast it has ended, so the
    next one starts in the project's workspace, and the pending call resolves into
    it. One shape for every runtime — Inkling deferred the call itself; a runtime
    that cannot be suspended was interrupted on the recorded decision and ended
    its turn as the same deferral."""

    tool_call_id: str
    project: str


@dataclass
class ReflexSuspender:
    """The reflex graph's `DeferredSuspender`: every deferral a run ends on comes
    through here once, and each kind goes where it is resolved — a `teleport` or
    a `bind` to the graph, which performs it and resumes the agent; anything else
    to a human, persisted as a batch and presented on the channel. React builds
    it with the run's context; Inkling reaches it through `ResolveDeferred`, a
    runtime a tool result cannot suspend through the `deferred_suspender` its run
    was handed.
    """

    channel: ChannelTentacle
    action_manager: DeferredActionManager
    conversation_manager: ConversationManager
    agent_tentacle_id: str
    run_name: RunName
    source_address: ChannelAddress
    target_address: ChannelAddress
    target_mode: ResponseTargetMode
    decision: SummonDecision | None
    thread_id: uuid.UUID | None = None
    emit_on_stream: bool = False
    suspended_batch_id: uuid.UUID | None = field(default=None, init=False)
    # Set when a run deferred a `teleport` (classified by metadata kind); the dispatch
    # graph reads this to route to its Teleport node instead of persisting a batch.
    teleport: TeleportRequest | None = field(default=None, init=False)
    # Set when a run deferred a `bind`: resolved by the graph too — the same agent,
    # resumed in place, in the workspace the bind made.
    bind: BindRequest | None = field(default=None, init=False)

    async def suspend(self, requests: DeferredToolRequests) -> ActionBatchEvent | None:
        # `teleport` declares kind="teleport" in its CallDeferred metadata — the graph
        # resolves it (fork + resume), not a human. Classify by the declared kind (not
        # a tool name), stash it typed, and let run1 end so it bubbles to the dispatch.
        for call in requests.calls:
            meta = requests.metadata.get(call.tool_call_id, {})
            if meta.get("kind") == BIND_DEFER_KIND:
                self.bind = BindRequest(
                    tool_call_id=call.tool_call_id,
                    project=str(meta.get("project") or ""),
                )
                return None
            if meta.get("kind") == TELEPORT_DEFER_KIND:
                # The gate names the far channel and the account on it as two plain
                # strings; the address is built back here, at the boundary, so the
                # node is handed a typed landing rather than a metadata dict.
                far = str(meta.get("channel") or "")
                self.teleport = TeleportRequest(
                    tool_call_id=call.tool_call_id,
                    hint=str(meta.get("hint") or ""),
                    crossing=CrossingLanding(
                        address=ChannelAddress(
                            channel_tentacle_id=far,
                            chat_type="dm",
                            chat_id="",
                            user_id=str(meta.get("user") or ""),
                        )
                    )
                    if far
                    else None,
                )
                return None
        with reflex_logfire.span(
            "suspend_for_review",
            run_name=self.run_name,
            agent_id=self.agent_tentacle_id,
            target_address=str(self.target_address),
            source_address=str(self.source_address),
            emit_on_stream=self.emit_on_stream,
        ) as span:
            if self.thread_id is None:
                raise ValueError("deferred review requires a thread_id")
            conversation = await self.conversation_manager.ensure(
                self.thread_id,
                agent_tentacle_id=self.agent_tentacle_id,
            )
            if self.emit_on_stream:
                # On-stream round-trip: persist the batch and hand it back as one
                # event for the consumer to render + mark as a unit.
                batch = await self.action_manager.create_batch(
                    conversation=conversation,
                    agent_tentacle_id=self.agent_tentacle_id,
                    run_name=self.run_name,
                    source_address=self.source_address,
                    target_address=self.target_address,
                    target_mode=self.target_mode,
                    decision=self.decision,
                    requests=requests,
                )
                self.suspended_batch_id = batch.id
                span.set_attribute("batch_id", str(batch.id))
                return ActionBatchEvent(
                    batch_id=str(batch.id),
                    questions=list(batch.questions),
                    approvals=list(batch.approvals),
                )

            batch = await self.channel.feelers.present_actions(
                action_manager=self.action_manager,
                conversation=conversation,
                agent_tentacle_id=self.agent_tentacle_id,
                run_name=self.run_name,
                source_address=self.source_address,
                target_address=self.target_address,
                target_mode=self.target_mode,
                decision=self.decision,
                requests=requests,
            )
            self.suspended_batch_id = batch.id
            span.set_attribute("batch_id", str(batch.id))
