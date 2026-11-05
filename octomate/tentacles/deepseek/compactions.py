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
from octomate.tentacles.deepseek.wire import ContentBlock, SessionEvent, text_of
from octomate.types.compactions import CompactionTrigger


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


class DeepseekCompactions:
    """Pair summaries with completed transformations before any presentation."""

    conversations: ConversationManager
    conversation: Conversation
    session_id: str
    source: str | None
    summaries: dict[str, tuple[CompactionSummary, SessionEvent]]

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

    async def consume(self, event: SessionEvent) -> None:
        if event.type not in {"compaction/summary", "compaction/end"}:
            return
        try:
            if event.type == "compaction/summary":
                summary = CompactionSummary.model_validate(event.data)
                self.summaries[summary.compaction_id] = summary, event
                return
            end = CompactionEnd.model_validate(event.data)
        except ValidationError:
            return
        pending = self.summaries.pop(end.compaction_id, None)
        if end.error is not None or pending is None:
            return
        summary, summary_event = pending
        timestamp = datetime.fromtimestamp(event.time / 1000, tz=UTC)
        messages: list[ModelRequest | ModelResponse] = [
            ModelRequest(
                parts=[UserPromptPart(text_of(summary.summary), timestamp=timestamp)],
                timestamp=timestamp,
                metadata=deepseek_metadata(
                    [
                        summary_event.model_dump(mode="json"),
                        event.model_dump(mode="json"),
                    ]
                ),
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
