"""Owned artifacts; a thread may reference one but does not own its content."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from arcanus import BaseTransmuter
from arcanus.base import Identity
from pydantic import UUID7, AnyHttpUrl, AwareDatetime, BaseModel, ConfigDict, Field
from uuid_utils.compat import uuid7

from octomate.models import artifacts as artifact_models
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.mcp import McpAppInvocation, McpAppResource


@sqlalchemy_materia.bless(artifact_models.Artifact)
class Artifact(BaseTransmuter):
    """A deliverable with its own identity and owner, requiring no thread.

    The artifact UUID identifies its viewer independently of any ledger row.
    Referencing it in a thread does not transfer ownership or grant content access.
    """

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    owner_id: UUID7 = Field(
        frozen=True,
        description="Registered owner; thread participation does not replace this ownership.",
    )
    title: str = Field(
        min_length=1, description="Display title captured when the artifact is created."
    )
    created_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When this artifact was recorded, in UTC.",
    )


@sqlalchemy_materia.bless(artifact_models.McpAppArtifact)
class McpAppArtifact(Artifact):
    """An MCP invocation retained for an authorized artifact viewer.

    The saved input and result support replay after an authorized UI resource
    fetch. The invocation's installation UUID never follows namespace
    reuse, and deleting a thread or uninstalling the provider does not delete it.
    """

    kind: Literal["mcp_app"] = Field(default="mcp_app", frozen=True)
    invocation: McpAppInvocation = Field(
        repr=False,
        description="Original call and complete result, kept outside generic ledger responses.",
    )


class McpAppView(BaseModel):
    """An authorized artifact snapshot and freshly fetched resource for replay."""

    artifact: McpAppArtifact = Field(
        description="The artifact and complete saved invocation."
    )
    resource: McpAppResource = Field(
        description="Untrusted HTML and resource UI metadata."
    )
    sandbox_origin: AnyHttpUrl = Field(
        description="Configured origin for the isolated sandbox."
    )
