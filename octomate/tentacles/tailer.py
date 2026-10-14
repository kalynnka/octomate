"""The shared lifecycle of a native turn published from a transcript."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from anyio.abc import ObjectSendStream
from pydantic import UUID7
from pydantic_ai.messages import (
    EnqueuedMessagesEvent,
    ModelMessage,
    ModelRequest,
    UserPromptPart,
)

from octomate.capabilities.harness.events import (
    RunEndedEvent,
    RunStartedEvent,
    StreamEvents,
    to_wire,
)
from octomate.managers.conversation import ConversationManager
from octomate.managers.thread import ThreadManager
from octomate.nerve import Impulse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.user import UserProfile
from octomate.tentacles.locks import SessionLocks
from octomate.types.json import JsonObject


@dataclass
class NativeTurn:
    """One transcript turn's live events and prompt bindings.

    Tailers supply native boundaries, source message identities, and their own
    accumulator's message list. This owner rejects committed or driven turns,
    records prompts in that list, publishes the fold, and settles only after the
    caller commits the run. Subagent transcripts do not publish on this surface.
    """

    threads: ThreadManager
    locks: SessionLocks
    address: ChannelAddress
    sender: UserProfile
    impulses: ObjectSendStream[Impulse]
    messages: list[ModelMessage]
    recorded: set[str]
    # Keep the actual requests: list positions can change while folding.
    prompts: dict[UUID7, ModelRequest] = field(default_factory=dict)

    @classmethod
    async def open(
        cls,
        *,
        run_id: str,
        recorded: set[str],
        conversations: ConversationManager,
        threads: ThreadManager,
        locks: SessionLocks,
        address: ChannelAddress,
        sender: UserProfile,
        impulses: ObjectSendStream[Impulse],
        messages: list[ModelMessage],
    ) -> NativeTurn | None:
        """Publish the opening bracket unless this turn already has an owner."""
        if run_id in recorded:
            return None
        if (
            await conversations.driven_run(
                address.channel_tentacle_id, address.chat_id, run_id
            )
            is not None
        ):
            recorded.add(run_id)
            return None
        turn = cls(threads, locks, address, sender, impulses, messages, recorded)
        await impulses.send(Impulse(RunStartedEvent(address=address), address))
        return turn

    async def prompt(
        self,
        *,
        message_id: str,
        text: str,
        timestamp: datetime,
        metadata: JsonObject | None = None,
    ) -> None:
        """Record a source prompt once and deliver its request at this point.

        The ledger row grants access before the run is complete. Its id is also
        the enqueue id, so the console reconciles the live prompt with that row.
        """
        async with self.locks.hold(self.address.chat_id):
            inbound = await self.threads.record_native_prompt(
                self.address,
                message_id=message_id,
                text=text,
                sender=self.sender,
                timestamp=timestamp,
            )
        request = ModelRequest(
            parts=[UserPromptPart(content=text, timestamp=timestamp)],
            timestamp=timestamp,
            metadata=metadata,
        )
        self.messages.append(request)
        self.prompts[inbound.id] = request
        await self.impulses.send(
            Impulse(
                EnqueuedMessagesEvent(enqueue_id=str(inbound.id), messages=(request,)),
                self.address,
            )
        )

    async def fold(self, events: Iterable[StreamEvents[str]]) -> None:
        """Consume and publish the accumulator's events on the native thread."""
        for event in events:
            if (wire := to_wire(event)) is not None:
                await self.impulses.send(Impulse(wire, self.address))

    async def close(
        self, run: ExternalAgentRun | None, *, run_id: str, answer: str
    ) -> None:
        """Bind the committed history before publishing the closing bracket.

        The caller holds the session lock through its commit and this binding.
        `record_external_run` preserves input order; resolve the request objects
        against that committed list here instead of tracking indices in tailers.
        A failed commit or binding propagates without publishing `run_ended`.
        """
        if run is None:
            self.recorded.add(run_id)
            return
        sources = {id(request): row_id for row_id, request in self.prompts.items()}
        prompts = {
            sources[id(message)]: stored.id
            for message, stored in zip(self.messages, run.messages, strict=True)
            if id(message) in sources
        }
        if len(prompts) != len(self.prompts):
            raise ValueError("A native prompt disappeared from the committed history")
        reply_id = await self.threads.bind_native_run(
            self.address, run, prompts=prompts, answer=answer, sender=self.sender
        )
        self.recorded.add(run_id)
        await self.impulses.send(
            Impulse(RunEndedEvent(thread_message_id=reply_id), self.address)
        )
