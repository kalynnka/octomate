from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from arcanus import BaseTransmuter, RelationCollection, Relationships
from arcanus.base import Identity
from pydantic import ConfigDict, Field

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

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[str, Identity]
    # The polymorphic discriminator: a distinct Literal per variant is what lets the
    # `AgentRun | ExternalAgentRun` union (e.g. `Conversation.runs`) resolve each row to
    # its type — mirroring pydantic-ai's own `ModelRequest | ModelResponse`. The
    # `external` variant narrows it; the override warns but is intended.
    kind: Literal["octomate"] = "octomate"
    conversation_id: uuid.UUID
    name: str | None = None
    cwd: str = Field(
        default="",
        description=(
            "The directory this run happened in, as its source reported it; empty "
            "when the source reported none, which is never a guess."
        ),
    )
    # The parent link of a subagent's turn, on the base so both variants carry it:
    # the run whose tool call spawned this one, and which ToolCallPart it answers.
    parent_run_id: str | None = None
    parent_tool_call_id: str | None = None
    started_at: datetime | None = None

    messages: RelationCollection[ModelRequest | ModelResponse] = Relationships()


@sqlalchemy_materia.bless(ExternalAgentRunModel)
class ExternalAgentRun(AgentRun):
    """A run rebuilt from an external runtime's transcript (native Claude session).
    Carries the transcript coordinates its offsets check-point ingest against."""

    kind: Literal["external"] = "external"  # pyright: ignore[reportIncompatibleVariableOverride]
    external_session_id: str | None = None
    source: str | None = None
    start_offset: int | None = None
    end_offset: int | None = None
    last_line_uuid: str | None = None
