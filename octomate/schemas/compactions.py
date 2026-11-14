"""Identities and provenance of completed context transformations."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from arcanus import BaseTransmuter, Identity, RelationCollection, Relationships
from pydantic import (
    UUID7,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)
from pydantic_ai._utils import now_utc
from uuid_utils.compat import uuid7

from octomate.models.compactions import Compaction as CompactionModel
from octomate.models.compactions import (
    CompactionMembership as CompactionMembershipModel,
)
from octomate.models.compactions import ExternalCompaction as ExternalCompactionModel
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.messages import ModelRequest, ModelResponse
from octomate.types.compactions import CompactionStage, CompactionTrigger


class NativeContext(BaseModel):
    """Source references at a completed boundary, without another content copy."""

    last_item_id: str
    replacement_ids: list[str]
    item_aliases: dict[str, str] = Field(
        default_factory=dict,
        description="Driven item IDs mapped to equivalent transcript IDs at this boundary.",
    )


class NativeMessage(BaseModel):
    """Identity metadata shared by the transcript and driven projections."""

    native_item_ids: list[str] = Field(default_factory=list)
    compaction_context: NativeContext | None = None


@sqlalchemy_materia.bless(CompactionModel)
class Compaction(BaseTransmuter):
    """A completed context transformation driven by Octomate.

    A successful no-op command or an unfinished attempt is not a compaction.
    Compact commands can transform context without producing a model run.
    """

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    kind: Literal["octomate"] = "octomate"
    conversation_id: UUID7 = Field(
        description="The conversation whose context was transformed; no model run is required.",
    )
    trigger: CompactionTrigger = Field(
        default="unknown",
        description="Reported cause: manual, auto, or unknown when the source does not say.",
    )
    happened_at: AwareDatetime | None = Field(
        default=None,
        description="Completion time reported by the source; None when unavailable.",
    )
    recorded_at: AwareDatetime = Field(
        default_factory=now_utc,
        description="When Octomate recorded the completed transformation, distinct from its source time.",
    )
    native_session_id: str | None = Field(
        default=None,
        description="Native session containing the transformation, when reported.",
    )
    native_compaction_id: str | None = Field(
        default=None,
        description="Native transformation identifier, when the source supplies one.",
    )
    membership_complete: bool = Field(
        default=False,
        description="Whether before and after membership describe the complete effective context; False means unknown or partial.",
    )
    messages: RelationCollection[ModelRequest | ModelResponse] = Relationships(
        description="Messages created or rewritten by this compaction; retained messages keep their original owner.",
    )

    def __lt__(self, other: Compaction) -> bool:
        return (self.recorded_at, self.id) < (other.recorded_at, other.id)

    def __gt__(self, other: Compaction) -> bool:
        return (self.recorded_at, self.id) > (other.recorded_at, other.id)

    def enrich_messages(self, messages: list[ModelRequest | ModelResponse]) -> None:
        """Add content once; later observations may supply its native references."""
        if not self.messages:
            self.messages.extend(messages)
            return
        if self.membership_complete or len(self.messages) != len(messages):
            return
        for stored, observed in zip(self.messages, messages, strict=True):
            if (
                stored.kind != observed.kind
                or stored.message_text != observed.message_text
                or (stored.message_text is None and stored.parts != observed.parts)
            ):
                continue
            if observed.metadata:
                merged = {**(stored.metadata or {}), **observed.metadata}
                if merged != stored.metadata:
                    stored.metadata = merged


@sqlalchemy_materia.bless(ExternalCompactionModel)
class ExternalCompaction(Compaction):
    """A completed transformation observed in an external runtime's transcript."""

    # Pydantic validates the subclass's own Literal; this override is valid at runtime.
    kind: Literal["external"] = "external"  # pyright: ignore[reportIncompatibleVariableOverride]
    source: str | None = Field(
        default=None,
        description="Transcript source containing the external compaction observation.",
    )
    start_offset: int | None = Field(
        default=None,
        ge=0,
        description="Inclusive byte offset of the transcript observation; None when unknown.",
    )
    end_offset: int | None = Field(
        default=None,
        ge=0,
        description="Exclusive byte offset of the transcript observation; None when unknown.",
    )

    @model_validator(mode="after")
    def validate_offsets(self) -> Self:
        if (
            self.start_offset is not None
            and self.end_offset is not None
            and self.end_offset < self.start_offset
        ):
            raise ValueError("end_offset must not precede start_offset")
        return self


type CompactionVariant = Annotated[
    Compaction | ExternalCompaction, Field(discriminator="kind")
]
CompactionAdapter = TypeAdapter(CompactionVariant)


@sqlalchemy_materia.bless(CompactionMembershipModel)
class CompactionMembership(BaseTransmuter):
    """An ordered reference to a message, with no copy or ownership transfer."""

    model_config = ConfigDict(from_attributes=True)

    compaction_id: Annotated[UUID7, Identity]
    stage: Annotated[CompactionStage, Identity] = Field(
        description="Before is the input context; after is the replacement context, including retained messages.",
    )
    position: Annotated[int, Identity] = Field(
        ge=0,
        description="Zero-based message position within this stage; independent of message timestamps and IDs.",
    )
    model_message_id: UUID7 = Field(
        description="Referenced archive message; its run or compaction ownership is unchanged and deletion is blocked while referenced.",
    )

    def __lt__(self, other: CompactionMembership) -> bool:
        return self.position < other.position

    def __gt__(self, other: CompactionMembership) -> bool:
        return self.position > other.position
