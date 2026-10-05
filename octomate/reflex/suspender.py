"""The reflex graph's deferred suspender: where a run's deferrals go — a teleport
back to the graph, everything else to a human as a persisted batch."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import UUID7
from pydantic_ai.tools import DeferredToolRequests

from octomate.capabilities.harness.events import ActionBatchEvent
from octomate.managers.conversation import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import DeferredActionBatch
from octomate.schemas.triage import (
    TELEPORT_DEFER_KIND,
    ResponseTargetMode,
    RunName,
    SummonDecision,
)
from octomate.telemetry import reflex_logfire
from octomate.tentacles.channel import ChannelTentacle
from octomate.types.deferred import DeferredResponseMode


@dataclass(frozen=True)
class TeleportRequest:
    """A teleport for the graph to perform: carry the history and resume the agent
    in the new place. One shape for every runtime — an Inkling run defers the
    `teleport` call itself; a runtime a tool result cannot suspend is interrupted
    on the recorded decision and ends its turn as the same deferral — read off
    the deferred call by its metadata kind."""

    hint: str
    # The deferred call to resolve into the resumed run.
    tool_call_id: str | None
    # Where the move goes: the chat a new thread opens in, or this conversation.
    destination: ChannelAddress
    new_thread: bool = True
    # The project the thread landed in is bound to, and the ref its workspace starts
    # from; None carries the conversation only.
    project: str | None = None
    ref: str | None = None
    # Whether the agent carries on at once where it lands.
    resume: bool = False
    # The user's next message where it lands, which the agent answers there.
    prompt: str | None = None

    @classmethod
    def of(cls, requests: DeferredToolRequests) -> TeleportRequest | None:
        """The teleport these requests carry."""
        for call in requests.calls:
            meta = requests.metadata.get(call.tool_call_id, {})
            if meta.get("kind") != TELEPORT_DEFER_KIND:
                continue
            destination = meta.get("destination")
            if not destination:
                raise ValueError("a teleport deferral names no destination")
            return cls(
                tool_call_id=call.tool_call_id,
                hint=str(meta.get("hint") or ""),
                new_thread=bool(meta.get("new_thread", True)),
                project=str(meta.get("project") or "") or None,
                ref=str(meta.get("ref") or "") or None,
                resume=bool(meta.get("resume", False)),
                # The address validates itself, being a pydantic dataclass.
                destination=ChannelAddress(**destination),
            )
        return None


@dataclass
class ReflexSuspender:
    """The reflex graph's `DeferredSuspender`: every deferral a run ends on comes
    through here once, and each kind goes where it is resolved — a `teleport` to
    the graph, which performs it and resumes the agent; anything else to a human,
    persisted as a batch and presented on the channel. React builds it with the
    run's context; Inkling reaches it through `ResolveDeferred`, a runtime a tool
    result cannot suspend through the `deferred_suspender` its run was handed.
    A runtime that asks a human mid-run pauses here instead, and stays live.
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
    model: str | None = None  # Selected model when a command has no routing decision.
    thread_id: UUID7 | None = None
    emit_on_stream: bool = False
    suspended_batch_id: UUID7 | None = field(default=None, init=False)
    # The deferred `teleport`, for the graph to perform instead of a batch.
    teleport: TeleportRequest | None = field(default=None, init=False)

    def continuation_decision(self) -> SummonDecision:
        """Preserve an actual agent run's selection for resume and teleport."""
        if self.decision is None:
            self.decision = SummonDecision(
                agent_id=self.agent_tentacle_id,
                model=self.model,
                reason="Continue the selected agent run.",
                hint="",
                summon="",
            )
        return self.decision

    async def suspend(self, requests: DeferredToolRequests) -> ActionBatchEvent | None:
        self.continuation_decision()
        teleport = TeleportRequest.of(requests)
        if teleport is not None:
            # The agent's own permission check already let the call through, so
            # nothing here asks: the graph moves it once the run ends.
            if len(requests.calls) + len(requests.approvals) > 1:
                raise RuntimeError("a teleport was deferred beside other calls")
            self.teleport = teleport
            return None
        with reflex_logfire.span(
            "suspend_for_review",
            run_name=self.run_name,
            agent_id=self.agent_tentacle_id,
            target_address=str(self.target_address),
            source_address=str(self.source_address),
            emit_on_stream=self.emit_on_stream,
        ) as span:
            batch = await self.persist(requests, response_mode="resume")
            self.suspended_batch_id = batch.id
            span.set_attribute("batch_id", str(batch.id))
            return await self.present(batch)

    async def pause(
        self, requests: DeferredToolRequests, *, batch_id: UUID7
    ) -> tuple[DeferredActionBatch, ActionBatchEvent | None]:
        with reflex_logfire.span(
            "pause_for_review",
            run_name=self.run_name,
            agent_id=self.agent_tentacle_id,
            target_address=str(self.target_address),
            emit_on_stream=self.emit_on_stream,
        ) as span:
            batch = await self.persist(
                requests, response_mode="live", batch_id=batch_id
            )
            span.set_attribute("batch_id", str(batch.id))
            return batch, await self.present(batch)

    async def persist(
        self,
        requests: DeferredToolRequests,
        *,
        response_mode: DeferredResponseMode,
        batch_id: UUID7 | None = None,
    ) -> DeferredActionBatch:
        if self.thread_id is None:
            raise ValueError("deferred review requires a thread_id")
        conversation = await self.conversation_manager.ensure(
            self.thread_id,
            agent_tentacle_id=self.agent_tentacle_id,
        )
        return await self.action_manager.create_batch(
            response_mode=response_mode,
            batch_id=batch_id,
            conversation=conversation,
            agent_tentacle_id=self.agent_tentacle_id,
            run_name=self.run_name,
            source_address=self.source_address,
            target_address=self.target_address,
            target_mode=self.target_mode,
            decision=self.decision,
            requests=requests,
        )

    async def present(self, batch: DeferredActionBatch) -> ActionBatchEvent | None:
        """The batch's event for the run's stream to present, or None once the
        channel presented it here because the run is not streamed."""
        event = ActionBatchEvent.from_batch(batch)
        if self.emit_on_stream:
            return event
        await self.channel.feelers.present_actions(
            self.target_address, event, action_manager=self.action_manager
        )
        return None
