"""Owned artifacts; a thread may reference one but does not own its content."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from arcanus import BaseTransmuter
from arcanus.base import Identity
from pydantic import (
    UUID7,
    AnyHttpUrl,
    AnyUrl,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)
from uuid_utils.compat import uuid7

from octomate.models import artifacts as artifact_models
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.mcp import McpAppInvocation, McpAppResource, McpAppResourceCSP


class ArtifactSandboxPolicy(BaseModel):
    """Public sandbox policy, tied to the embedding console by frame-ancestors.

    Sources covering the console hostname are refused, including wildcards and
    alternate schemes or ports. HTML and invocation data never travel in this
    policy URL; the authenticated viewer sends them over the sandbox bridge.
    """

    parent_origin: AnyHttpUrl = Field(description="The embedding console's origin.")
    csp: McpAppResourceCSP = Field(
        default_factory=McpAppResourceCSP, description="Validated resource allowances."
    )

    @model_validator(mode="after")
    def protect_console(self) -> "ArtifactSandboxPolicy":
        parent = self.parent_origin
        if (
            parent.username is not None
            or parent.password is not None
            or parent.path not in {None, "/"}
            or parent.query is not None
            or parent.fragment is not None
            or parent.host is None
        ):
            raise ValueError("The sandbox parent must be an origin")
        for sources in (
            self.csp.connect_domains,
            self.csp.resource_domains,
            self.csp.frame_domains,
            self.csp.base_uri_domains,
        ):
            for source in sources or []:
                host = AnyUrl(source).host
                if host == parent.host or (
                    host is not None
                    and host.startswith("*.")
                    and parent.host.endswith(host[1:])
                ):
                    raise ValueError("Artifact policy must not allow the console host")
        return self


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
    sandbox_url: AnyHttpUrl = Field(
        description="Sandbox proxy URL carrying the validated public policy only."
    )
    viewer_path: str = Field(
        description="Root-relative standalone artifact viewer URL."
    )
