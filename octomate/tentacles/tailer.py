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
    TextContent,
    UserPromptPart,
)

from octomate.events import (
    AgentMessageEvent,
    RunActivityChangedEvent,
    RunEndedEvent,
    RunStartedEvent,
    StreamEvents,
    SystemMessageEvent,
    to_wire,
)
from octomate.managers.conversation import ConversationManager
from octomate.managers.thread import ThreadManager
from octomate.nerve import Impulse
from octomate.schemas.annotations import MessageAnnotations
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.user import UserProfile
from octomate.tentacles.locks import SessionLocks
from octomate.types.json import JsonObject
from octomate.types.threads import ChannelActorKind


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
    replay: bool = (
        False  # True until the connection's initial transcript offset is reached.
    )

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
        replay: bool = False,
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
        turn = cls(
            threads, locks, address, sender, impulses, messages, recorded, replay=replay
        )
        await impulses.send(
            Impulse(
                RunStartedEvent(address=address, sender=sender), address, replay=replay
            )
        )
        return turn

    async def caught_up(self) -> None:
        """Resume activity for an open turn after restoring its historical prefix."""
        self.replay = False
        await self.impulses.send(
            Impulse(RunActivityChangedEvent(activity="working"), self.address)
        )

    async def prompt(
        self,
        *,
        message_id: str,
        text: str | TextContent,
        timestamp: datetime,
        metadata: JsonObject | None = None,
        actor_kind: ChannelActorKind = "human",
    ) -> None:
        """Record a source prompt once and deliver its request at this point.

        The ledger row grants access before the run is complete. Its id is also
        the enqueue id, so the console reconciles the live prompt with that row.
        A line the harness wrote in the person's place is a `system` row: it joins
        the history and the ledger, and a reader is told the harness said it.
        A prompt whose annotations name an agent is that agent's message.
        """
        agent = (
            MessageAnnotations.model_validate(text.metadata["annotations"]).agent
            if isinstance(text, TextContent)
            else None
        )
        async with self.locks.hold(self.address.chat_id):
            inbound = await self.threads.record_native_prompt(
                self.address,
                message_id=message_id,
                text=text,
                sender=self.sender,
                timestamp=timestamp,
                actor_kind="agent" if agent is not None else actor_kind,
            )
        request = ModelRequest(
            parts=[
                UserPromptPart(
                    content=[text] if isinstance(text, TextContent) else text,
                    timestamp=timestamp,
                )
            ],
            timestamp=timestamp,
            metadata=metadata,
        )
        self.messages.append(request)
        self.prompts[inbound.id] = request
        if agent is not None:
            await self.impulses.send(
                Impulse(
                    AgentMessageEvent(
                        message_id=inbound.id,
                        agent=agent,
                        text=inbound.message_text or "",
                    ),
                    self.address,
                    replay=self.replay,
                )
            )
        elif actor_kind == "human":
            await self.impulses.send(
                Impulse(
                    EnqueuedMessagesEvent(
                        enqueue_id=str(inbound.id), messages=(request,)
                    ),
                    self.address,
                    replay=self.replay,
                )
            )
        elif actor_kind == "system":
            await self.impulses.send(
                Impulse(
                    SystemMessageEvent(
                        message_id=inbound.id, text=inbound.message_text or ""
                    ),
                    self.address,
                    replay=self.replay,
                )
            )

    async def fold(self, events: Iterable[StreamEvents[str]]) -> None:
        """Consume and publish the accumulator's events on the native thread."""
        for event in events:
            if (wire := to_wire(event)) is not None:
                await self.impulses.send(
                    Impulse(wire, self.address, replay=self.replay)
                )

    async def close(
        self,
        run: ExternalAgentRun | None,
        *,
        run_id: str,
        answer: str,
        interrupted: bool = False,
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
        await self.settle(run_id, thread_message_id=reply_id, interrupted=interrupted)

    async def settle(
        self,
        run_id: str,
        *,
        thread_message_id: UUID7 | None = None,
        interrupted: bool = False,
    ) -> None:
        """Publish the closing bracket after the native task's durable writes finish."""
        self.recorded.add(run_id)
        await self.impulses.send(
            Impulse(
                RunEndedEvent(
                    thread_message_id=thread_message_id,
                    outcome="cancelled" if interrupted else None,
                ),
                self.address,
                replay=self.replay,
            )
        )
