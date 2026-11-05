"""Completed dsh summaries shared by driven execution and native log ingestion."""

from datetime import UTC, datetime

from octomate_protocol.deepseek import PERMISSIVE
from pydantic import BaseModel, Field, ValidationError
from pydantic_ai.messages import UserPromptPart

from octomate.managers.conversation import ConversationManager
from octomate.schemas.conversation import Conversation
from octomate.schemas.messages import ModelRequest, ModelResponse
from octomate.schemas.thread import DEEPSEEK_NATIVE_ID
from octomate.tentacles.deepseek.adapter import deepseek_metadata
from octomate.tentacles.deepseek.wire import (
    ContentBlock,
    SessionEvent,
    UserMessageData,
    text_of,
)
from octomate.types.compactions import CompactionTrigger
from octomate.types.json import JsonValue


class CompactionSummary(BaseModel):
    """A prepared summary; completion still requires a matching successful end."""

    model_config = PERMISSIVE

    compaction_id: str = Field(alias="compactionId")
    summary: list[ContentBlock]


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
                native_id=DEEPSEEK_NATIVE_ID,
                native_session_id=self.session_id,
                native_compaction_id=end.compaction_id,
                trigger=trigger,
                happened_at=timestamp,
                messages=messages,
            )
            return
        await self.conversations.record_external_compaction(
            self.conversation,
            native_id=DEEPSEEK_NATIVE_ID,
            native_session_id=self.session_id,
            native_compaction_id=end.compaction_id,
            trigger=trigger,
            source=self.source,
            start_offset=None,
            end_offset=None,
            happened_at=timestamp,
            messages=messages,
        )
