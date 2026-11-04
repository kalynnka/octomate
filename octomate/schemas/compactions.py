"""Identities and provenance of completed context transformations."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from arcanus import BaseTransmuter
from arcanus.base import Identity
from pydantic import (
    UUID7,
    AwareDatetime,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)
from pydantic_ai._utils import now_utc
from uuid_utils.compat import uuid7

from octomate.models.compactions import Compaction as CompactionModel
from octomate.models.compactions import ExternalCompaction as ExternalCompactionModel
from octomate.schemas.base import sqlalchemy_materia
from octomate.types.compactions import CompactionTrigger


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
    native_id: str | None = Field(
        default=None,
        description="Native runtime owning the source identifiers; None for transformations without one.",
    )
    native_session_id: str | None = Field(
        default=None,
        description="Native session containing the transformation, when reported.",
    )
    native_compaction_id: str | None = Field(
        default=None,
        description="Native transformation identifier, when the source supplies one.",
    )


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
