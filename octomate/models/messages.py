"""The model messages table, polymorphic on request and response."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, Literal

from arcanus.base import TransmuterProxiedMixin
from pydantic import UUID7, JsonValue
from sqlalchemy import JSON, CheckConstraint, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_utils.compat import uuid7

from octomate.models.base import Base, MapperArgs, UTCDateTime

if TYPE_CHECKING:
    from octomate.models.compactions import Compaction
    from octomate.models.runs import AgentRun
    from octomate.models.thread import ThreadMessage


class ModelMessage(Base, TransmuterProxiedMixin):
    """The polymorphic base of the model messages table; every row is a request
    or a response."""

    __tablename__ = "model_messages"
    __table_args__ = (
        CheckConstraint(
            "(run_id IS NOT NULL AND compaction_id IS NULL) OR "
            "(run_id IS NULL AND compaction_id IS NOT NULL)",
            name="ck_model_messages_single_owner",
        ),
    )
    __mapper_args__: ClassVar[MapperArgs] = {
        "polymorphic_on": "kind",
        "polymorphic_abstract": True,
    }

    id: Mapped[UUID7] = mapped_column(Uuid, primary_key=True, default=uuid7)
    run_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    compaction_id: Mapped[UUID7 | None] = mapped_column(
        Uuid,
        ForeignKey(
            "compactions.id",
            name="fk_model_messages_compaction_id_compactions",
            ondelete="CASCADE",
        ),
        nullable=True,
        index=True,
        comment="Compaction that created or rewrote this message; NULL for original run messages.",
    )

    kind: Mapped[str] = mapped_column(String, nullable=False, index=True)
    parts: Mapped[JsonValue] = mapped_column(JSON, nullable=False)
    timestamp: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True, index=True
    )
    # `metadata` is reserved by SQLAlchemy's DeclarativeBase for the table
    # MetaData; expose the column as `meta` on Python and `metadata` in the DB.
    meta: Mapped[JsonValue] = mapped_column("metadata", JSON, nullable=True)

    instructions: Mapped[str | None] = mapped_column(String, nullable=True)

    usage: Mapped[JsonValue] = mapped_column(JSON, nullable=True)
    model_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="Configured model identifier used for the request, distinct from the provider-reported model name.",
    )
    model_name: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    provider_name: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    provider_url: Mapped[str | None] = mapped_column(String, nullable=True)
    canonical_system: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="Provider system whose keys validate this response's signed or encrypted content, when distinct from provider_name.",
    )
    provider_details: Mapped[JsonValue] = mapped_column(JSON, nullable=True)
    provider_response_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )
    workspace_ref: Mapped[JsonValue] = mapped_column(
        JSON,
        nullable=True,
        comment="The provider and identifier of the response's workspace, without credentials.",
    )
    history_digest: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="Digest of the response and preceding history used to validate server-side continuation.",
    )
    failed_attempts: Mapped[JsonValue] = mapped_column(
        JSON,
        nullable=True,
        comment="Earlier model request attempts that failed before this response was produced, in order.",
    )
    finish_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    # `state` is response-only on pydantic-ai's side ('complete' | 'interrupted'),
    # but lives on the shared table since responses and requests share the same
    # polymorphic table. Defaulted to 'complete' so requests have a sane value.
    state: Mapped[str] = mapped_column(
        String, nullable=False, default="complete", index=True
    )
    conversation_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )
    # Derived at the schema boundary (see octomate.schemas.messages): `role`
    # tags the human-facing sender; `message_text` is the flattened plain text of
    # the user/assistant message (None for tool-only turns), the search substrate.
    role: Mapped[Literal["user", "assistant"]] = mapped_column(
        String, nullable=False, server_default="assistant", index=True
    )
    message_text: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    run: Mapped[AgentRun | None] = relationship(
        "AgentRun", back_populates="messages", lazy="raise_on_sql"
    )
    compaction: Mapped[Compaction | None] = relationship(
        "Compaction", back_populates="messages", lazy="raise_on_sql"
    )
    thread_messages: Mapped[list[ThreadMessage]] = relationship(
        "ThreadMessage",
        secondary="message_binding",
        back_populates="model_messages",
        order_by="(ThreadMessage.happened_at, ThreadMessage.id)",
        lazy="raise_on_sql",
        viewonly=True,
        overlaps="thread_message,model_message",
    )


class ModelRequest(ModelMessage):
    """A request row."""

    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_identity": "request"}


class ModelResponse(ModelMessage):
    """A response row."""

    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_identity": "response"}
