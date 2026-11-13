"""Completed dsh summaries shared by driven execution and native log ingestion."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import NamedTuple

from octomate_protocol.deepseek import PERMISSIVE
from pydantic import UUID7, BaseModel, Field, ValidationError
from pydantic_ai.messages import UserPromptPart

from octomate.managers.conversation import (
    ConversationManager,
    IncompleteCompactionContext,
)
from octomate.schemas.compactions import Compaction
from octomate.schemas.conversation import Conversation
from octomate.schemas.messages import ModelRequest, ModelResponse
from octomate.tentacles.deepseek.adapter import deepseek_metadata
from octomate.tentacles.deepseek.wire import (
    ContentBlock,
    SessionAssistantFrame,
    SessionEvent,
    UserMessageData,
    text_of,
)
from octomate.types.compactions import CompactionRewrite, CompactionTrigger
from octomate.types.json import JsonValue


class CompactionSummary(BaseModel):
    """A prepared summary; completion still requires a matching successful end."""

    model_config = PERMISSIVE

    compaction_id: str = Field(alias="compactionId")
    summary: list[ContentBlock]
    shadowed_seqs: list[int] | None = Field(default=None, alias="shadowedSeqs")


class MessageProvenance(BaseModel):
    """The existing event metadata needed to map native surface identities."""

    events: list[SessionEvent | SessionAssistantFrame] = Field(default_factory=list)


class SurfaceMessage(NamedTuple):
    """One stored message may fold several indivisible native surface entries."""

    message_id: UUID7
    seqs: list[int]


class SurfaceCompaction(NamedTuple):
    """A completed checkpoint and the exact native span it replaces."""

    record: Compaction
    shadowed_seqs: list[int]
    seq: int
    checkpoint: SurfaceMessage

    def __lt__(self, other: SurfaceCompaction) -> bool:
        return self.seq < other.seq

    def __gt__(self, other: SurfaceCompaction) -> bool:
        return self.seq > other.seq

    def __le__(self, other: SurfaceCompaction) -> bool:
        return self.seq <= other.seq

    def __ge__(self, other: SurfaceCompaction) -> bool:
        return self.seq >= other.seq


class CompactionEnd(BaseModel):
    """The durable close of a standalone or in-turn compaction attempt."""

    model_config = PERMISSIVE

    compaction_id: str = Field(alias="compactionId")
    source_command_id: str | None = Field(default=None, alias="sourceCommandId")
    turn: int | None
    error: str | None = None


class CompactionCheckpointSource(BaseModel):
    """The source identity on the concrete replacement user message."""

    model_config = PERMISSIVE

    compaction_id: str = Field(alias="compactionId")


class DeepseekCompactions:
    """Pair summaries with completed transformations before any presentation."""

    conversations: ConversationManager
    conversation: Conversation
    session_id: str
    source: str | None
    summaries: dict[str, tuple[CompactionSummary, SessionEvent]]
    checkpoints: dict[str, tuple[UserMessageData, SessionEvent]]
    pending_membership: str | None

    def __init__(
        self,
        conversations: ConversationManager,
        conversation: Conversation,
        session_id: str,
        *,
        source: str | None = None,
    ) -> None:
        self.conversations = conversations
        self.conversation = conversation
        self.session_id = session_id
        self.source = source
        self.summaries = {}
        self.checkpoints = {}
        self.pending_membership = None

    async def consume(self, event: SessionEvent) -> None:
        if event.type not in {"compaction/summary", "compaction/end", "user/message"}:
            return
        try:
            if event.type == "user/message":
                checkpoint = UserMessageData.model_validate(event.data)
                if checkpoint.source.kind == "compact-checkpoint":
                    source = CompactionCheckpointSource.model_validate(
                        checkpoint.source.model_dump(by_alias=True)
                    )
                    self.checkpoints[source.compaction_id] = checkpoint, event
                return
            if event.type == "compaction/summary":
                summary = CompactionSummary.model_validate(event.data)
                self.summaries[summary.compaction_id] = summary, event
                return
            end = CompactionEnd.model_validate(event.data)
        except ValidationError:
            return
        pending = self.summaries.pop(end.compaction_id, None)
        checkpoint = self.checkpoints.pop(end.compaction_id, None)
        if end.error is not None or pending is None:
            return
        summary, summary_event = pending
        timestamp = datetime.fromtimestamp(event.time / 1000, tz=UTC)
        content = text_of(
            checkpoint[0].content if checkpoint is not None else summary.summary
        )
        provenance: list[JsonValue] = [summary_event.model_dump(mode="json")]
        if checkpoint is not None:
            provenance.append(checkpoint[1].model_dump(mode="json"))
        provenance.append(event.model_dump(mode="json"))
        messages: list[ModelRequest | ModelResponse] = [
            ModelRequest(
                parts=[UserPromptPart(content, timestamp=timestamp)],
                timestamp=timestamp,
                metadata=deepseek_metadata(provenance),
            )
        ]
        trigger: CompactionTrigger = (
            "manual"
            if end.source_command_id is not None
            else "auto"
            if end.turn is not None
            else "unknown"
        )
        if self.source is None:
            await self.conversations.record_driven_compaction(
                self.conversation,
                native_session_id=self.session_id,
                native_compaction_id=end.compaction_id,
                trigger=trigger,
                happened_at=timestamp,
                messages=messages,
            )
        else:
            await self.conversations.record_external_compaction(
                self.conversation,
                native_session_id=self.session_id,
                native_compaction_id=end.compaction_id,
                trigger=trigger,
                source=self.source,
                start_offset=None,
                end_offset=None,
                happened_at=timestamp,
                messages=messages,
            )
        self.pending_membership = end.compaction_id

    async def record_memberships(self) -> None:
        """Map exact surface spans once their enclosing run is durable.

        Missing source identities or a span splitting a normalized message remain
        unknown. Sequence numbers are interpreted only within their native session.
        """
        pending = self.pending_membership
        if pending is None:
            return
        self.pending_membership = None
        records = await self.conversations.native_compactions(
            self.conversation, self.session_id
        )
        owner = next(
            row.conversation_id
            for row in records
            if row.native_compaction_id == pending
        )
        pending_records = [
            row
            for row in records
            if row.conversation_id == owner and not row.membership_complete
        ]
        if not pending_records:
            return
        boundaries: list[SurfaceCompaction] = []
        for row in pending_records:
            if len(row.messages) != 1:
                return
            message = row.messages[0]
            events = MessageProvenance.model_validate(message.metadata or {}).events
            summary_event = next(
                (
                    event
                    for event in events
                    if isinstance(event, SessionEvent)
                    and event.type == "compaction/summary"
                ),
                None,
            )
            checkpoint = next(
                (
                    event
                    for event in events
                    if isinstance(event, SessionEvent) and event.type == "user/message"
                ),
                None,
            )
            if summary_event is None or checkpoint is None:
                return
            summary = CompactionSummary.model_validate(summary_event.data)
            if not summary.shadowed_seqs:
                return
            boundaries.append(
                SurfaceCompaction(
                    row,
                    summary.shadowed_seqs,
                    summary_event.seq,
                    SurfaceMessage(message.id, [checkpoint.seq]),
                )
            )
        conversation = await self.conversations.get(owner, with_runs=False)
        session_runs = await self.conversations.session_run_ids(
            conversation, self.session_id
        )
        current_checkpoints = {row.id for row in records}
        try:
            context = await self.conversations.effective_messages(
                conversation, pending=tuple(row.id for row in pending_records)
            )
        except IncompleteCompactionContext:
            return
        archive: list[SurfaceMessage] = []
        injected: set[int] = set()
        for message in context:
            if (message.run_id is not None and message.run_id not in session_runs) or (
                message.compaction_id is not None
                and message.compaction_id not in current_checkpoints
            ):
                archive.append(SurfaceMessage(message.id, []))
                continue
            events = MessageProvenance.model_validate(message.metadata or {}).events
            injected.update(
                event.seq
                for event in events
                if isinstance(event, SessionEvent)
                and event.type == "user/message"
                and UserMessageData.model_validate(event.data).source.kind
                not in {"user", "user-question-reply", "compact-checkpoint"}
            )
            seqs = sorted(
                {
                    event.seq
                    for event in events
                    if isinstance(event, SessionEvent)
                    and (
                        event.type in {"assistant/message", "tool/result"}
                        or (
                            event.type == "user/message"
                            and UserMessageData.model_validate(event.data).source.kind
                            in (
                                {"compact-checkpoint"}
                                if message.compaction_id is not None
                                else {"user", "user-question-reply"}
                            )
                        )
                    )
                }
            )
            if not seqs:
                return
            archive.append(SurfaceMessage(message.id, seqs))
        effective: list[SurfaceMessage] = []
        rewrites: list[CompactionRewrite] = []
        cursor = 0
        for boundary in sorted(boundaries):
            while cursor < len(archive) and (
                not archive[cursor].seqs or archive[cursor].seqs[-1] < boundary.seq
            ):
                effective.append(archive[cursor])
                cursor += 1
            # Injected context is archived as provenance, not normalized messages.
            shadowed_seqs = [
                seq for seq in boundary.shadowed_seqs if seq not in injected
            ]
            shadowed = set(shadowed_seqs)
            indices = [
                index
                for index, item in enumerate(effective)
                if any(seq in shadowed for seq in item.seqs)
            ]
            if not indices:
                break
            start, stop = indices[0], indices[-1] + 1
            if [
                seq for item in effective[start:stop] for seq in item.seqs
            ] != shadowed_seqs:
                break
            before = [item.message_id for item in effective]
            effective[start:stop] = [boundary.checkpoint]
            rewrites.append(
                CompactionRewrite(
                    boundary.record.id, before, [item.message_id for item in effective]
                )
            )
        await self.conversations.record_compaction_memberships(conversation, rewrites)
