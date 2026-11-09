"""Agent runs: one per `agent.run()`, driven by Octomate or replayed from an
external transcript."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from arcanus import BaseTransmuter, RelationCollection, Relationships
from arcanus.base import Identity
from pydantic import UUID7, AwareDatetime, ConfigDict, Field

from octomate.models.runs import AgentRun as AgentRunModel
from octomate.models.runs import ExternalAgentRun as ExternalAgentRunModel
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.messages import ModelRequest, ModelResponse


@sqlalchemy_materia.bless(AgentRunModel)
class AgentRun(BaseTransmuter):
    """One agent.run() invocation. PK is the pydantic-ai run_id (a uuid7 string).

    The `octomate` variant of the polymorphic run — driven by Octomate. A native
    run replayed from an external transcript is the `ExternalAgentRun` variant.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: Annotated[str, Identity]
    # The polymorphic discriminator: a distinct Literal per variant is what lets the
    # `AgentRun | ExternalAgentRun` union (e.g. `Conversation.runs`) resolve each row to
    # its type — mirroring pydantic-ai's own `ModelRequest | ModelResponse`. The
    # `external` variant narrows it; the override warns but is intended.
    kind: Literal["octomate"] = "octomate"
    name: str | None = None
    agent_name: str | None = Field(
        default=None,
        description="Harness agent identity, distinct from the run's display name.",
    )
    registration_id: str | None = Field(
        default=None,
        description="Durable registration invocation that claimed this run.",
    )
    started_at: AwareDatetime | None = None

    conversation_id: UUID7
    # The parent link of a subagent's turn, on the base so both variants carry it:
    # the run whose tool call spawned this one, and which ToolCallPart it answers.
    parent_run_id: str | None = None
    parent_tool_call_id: str | None = None

    model_name: str | None = None
    context_window: int | None = Field(
        default=None,
        gt=0,
        description="Context capacity in tokens reported by the harness for this run; None when unknown.",
    )
    context_used: int | None = Field(
        default=None,
        ge=0,
        description="Context tokens used, from the harness occupancy snapshot or the last model request's input usage; None when unavailable.",
    )
    permission_mode: str | None = None
    cwd: Path | None = Field(
        default=None,
        description=(
            "The directory this run happened in, as its source reported it — spelled "
            "by `Path`, so a trailing slash or a `.` segment is normalized away. None "
            "when the source reported none, which is never a guess."
        ),
    )

    native_session_id: str | None = None
    native_turn_id: str | None = Field(
        default=None,
        description="The native ingest turn key used to recognize a replayed driven run.",
    )

    metadata: dict[str, str] | None = Field(
        default=None,
        alias="meta",
        description="Harness registration metadata; None means unregistered, while an empty dictionary is a valid registration.",
    )

    messages: RelationCollection[ModelRequest | ModelResponse] = Relationships()


@sqlalchemy_materia.bless(ExternalAgentRunModel)
class ExternalAgentRun(AgentRun):
    """A run rebuilt from an external runtime's transcript (native Claude session).
    Carries the transcript coordinates its offsets check-point ingest against."""

    kind: Literal["external"] = "external"  # pyright: ignore[reportIncompatibleVariableOverride]
    source: str | None = None
    start_offset: int | None = None
    end_offset: int | None = None
    last_line_uuid: str | None = None
