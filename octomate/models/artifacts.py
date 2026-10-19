"""Agent deliverables with identities and ownership independent of chat history."""

from datetime import UTC, datetime
from typing import ClassVar

from arcanus.base import TransmuterProxiedMixin
from pydantic import UUID7, JsonValue
from sqlalchemy import JSON, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_utils.compat import uuid7

from octomate.models.base import Base, MapperArgs, UTCDateTime
from octomate.models.files import Html


class Artifact(Base, TransmuterProxiedMixin):
    """An owned deliverable, polymorphic on the content its viewer loads."""

    __tablename__ = "artifacts"
    __mapper_args__: ClassVar[MapperArgs] = {
        "polymorphic_on": "kind",
        "polymorphic_abstract": True,
        "with_polymorphic": "*",
    }

    id: Mapped[UUID7] = mapped_column(Uuid, primary_key=True, default=uuid7)
    owner_id: Mapped[UUID7] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="Registered owner, independent of the threads referencing this artifact.",
    )
    kind: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Content variant determining the artifact viewer.",
    )
    title: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Display title captured when the artifact is created.",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="When this artifact was recorded, in UTC.",
    )


class McpAppArtifact(Artifact):
    """An MCP app restored from its saved invocation and authorized UI resource."""

    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_identity": "mcp_app"}

    invocation: Mapped[JsonValue] = mapped_column(
        JSON,
        nullable=True,
        comment=(
            "MCP app invocation: original installation identity, tool declaration, "
            "UI resource URI, arguments, and complete result. Retained after uninstall."
        ),
    )


class HtmlArtifact(Artifact):
    """A captured HTML visualization independent of its source file."""

    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_identity": "html"}

    file_id: Mapped[UUID7] = mapped_column(
        Uuid,
        ForeignKey("files.id", name="fk_artifacts_file_id_files", ondelete="RESTRICT"),
        nullable=True,
        comment="Owned HTML file captured from the run workspace for isolated rendering.",
    )
    file: Mapped[Html] = relationship("Html", lazy="selectin")
