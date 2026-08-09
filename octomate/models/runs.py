from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from octomate.models.base import Base, MapperArgs

if TYPE_CHECKING:
    from octomate.models.conversation import Conversation
    from octomate.models.messages import ModelMessage


class AgentRun(Base, TransmuterProxiedMixin):
    """One agent.run() invocation within a Conversation.

    `id` is the pydantic-ai run_id (uuid7-as-string), which already lives on
    every ModelMessage produced during the run — so messages reference it as
    a foreign key without inventing a synthetic surrogate.

    Polymorphic base of the run variants (single-table): the plain `octomate`
    identity is an Octomate-driven run; `ExternalAgentRun` is a run replayed from an
    external runtime's transcript (native Claude), carrying its transcript coordinates.
    """

    __tablename__ = "agent_runs"
    __mapper_args__: ClassVar[MapperArgs] = {
        "polymorphic_on": "kind",
        "polymorphic_identity": "octomate",
        # Load every variant's columns on a base-class query or relationship (e.g.
        # `Conversation.runs`) — else the subclass columns are deferred and read back
        # NULL. Free for single-table: no join, just a wider SELECT.
        "with_polymorphic": "*",
    }

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(
        String, nullable=False, server_default="octomate", index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        default=None,
        index=True,
    )
    cwd: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
        comment=(
            "The directory this run happened in, as its source reported it; empty "
            "when the source reported none. On the base because an Octomate-driven "
            "run knows its directory too — where a run ran is not a per-variant "
            "question."
        ),
    )
    parent_run_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        default=None,
        index=True,
        comment=(
            "The run whose tool call spawned this one (a subagent's turn). An "
            "unenforced reference, not a FK: native ingest supersedes a provisional "
            "parent by deleting and reinserting the same id across two commits, "
            "which an enforced constraint would block or cascade."
        ),
    )
    parent_tool_call_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        default=None,
        comment=(
            "The ToolCallPart in the parent run's timeline this run answers, so a "
            "tool call can expand into its subagent's full timeline."
        ),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )

    conversation: Mapped[Conversation] = relationship(
        "Conversation", back_populates="runs"
    )
    messages: Mapped[list[ModelMessage]] = relationship(
        "ModelMessage",
        back_populates="run",
        cascade="all",
        order_by="ModelMessage.id",
        lazy="selectin",
    )


class ExternalAgentRun(AgentRun):
    """A run rebuilt from an external runtime's transcript (native Claude session) —
    the `external` polymorphic identity.

    Single-table inheritance: SQLAlchemy places these on the shared `agent_runs` table
    as nullable columns (octomate runs leave them NULL — the STI tradeoff), and the
    base's `with_polymorphic` loads them on a base-class read. `start_offset` /
    `end_offset` are a turn's byte range in the transcript, the checkpoint a live tailer
    and recovery resume from.
    """

    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_identity": "external"}

    external_session_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    start_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_line_uuid: Mapped[str | None] = mapped_column(String, nullable=True)
