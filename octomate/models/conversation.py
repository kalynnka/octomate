"""The conversations table: one agent's context within a thread."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

from arcanus.base import TransmuterProxiedMixin
from pydantic import UUID7
from pydantic_ai import DeferredToolRequests
from pydantic_ai.usage import RunUsage
from sqlalchemy import (
    ARRAY,
    JSON,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_utils.compat import uuid7

from octomate.models.base import Base, MapperArgs
from octomate.types.permissions import AgentPermissionMode

if TYPE_CHECKING:
    from octomate.models.files import File
    from octomate.models.runs import AgentRun
    from octomate.models.thread import Thread


class ConversationRun(Base, TransmuterProxiedMixin):
    """A run in a conversation's history. Every run is in the history of the
    conversation it ran in; a fork adds the runs it was forked with, so the fork
    and its source read the same rows."""

    __tablename__ = "conversation_runs"

    conversation_id: Mapped[UUID7] = mapped_column(
        Uuid,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    run_id: Mapped[str] = mapped_column(
        String,
        ForeignKey(
            "agent_runs.id",
            name="fk_conversation_runs_run_id_agent_runs",
            ondelete="NO ACTION",
        ),
        primary_key=True,
        index=True,
        comment=(
            "Not cascaded: a run cannot be deleted while any history includes it, so "
            "a conversation whose runs a fork still includes cannot be deleted. Not "
            "RESTRICT either, which SQLite checks mid-statement, before the deleted "
            "conversation's own references are sure to be gone."
        ),
    )


class ConversationCompaction(Base, TransmuterProxiedMixin):
    """A compaction in a conversation's own or frozen inherited history."""

    __tablename__ = "conversation_compactions"
    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_conversation_compactions_position"),
        UniqueConstraint(
            "conversation_id", "position", name="uq_conversation_compactions_position"
        ),
    )

    conversation_id: Mapped[UUID7] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    compaction_id: Mapped[UUID7] = mapped_column(
        Uuid,
        ForeignKey(
            "compactions.id",
            name="fk_conversation_compactions_compaction",
            ondelete="NO ACTION",
        ),
        primary_key=True,
        index=True,
        comment="Shared compaction identity; deletion is blocked while a conversation still includes it.",
    )
    position: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Zero-based compaction order in this conversation; NULL when the order has not been established.",
    )


class Conversation(Base, TransmuterProxiedMixin):
    """One agent's context within a thread, unique per (thread, agent, subagent)."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint(
            "thread_id",
            "agent_tentacle_id",
            "subagent_id",
            name="uq_conversations_conversation_key",
        ),
    )

    id: Mapped[UUID7] = mapped_column(Uuid, primary_key=True, default=uuid7)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    transcript_file_id: Mapped[UUID7 | None] = mapped_column(
        Uuid,
        ForeignKey(
            "files.id",
            name="fk_conversations_transcript_file_id_files",
            ondelete="SET NULL",
        ),
        nullable=True,
        comment="Latest uploaded native transcript; independent conversations do not share this reference.",
    )

    thread_id: Mapped[UUID7] = mapped_column(
        Uuid,
        ForeignKey("threads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment=(
            "The owning thread. A thread owns one conversation per agent plus one "
            "per that agent's subagents, so (thread_id, agent_tentacle_id, "
            "subagent_id) is the conversation's identity."
        ),
    )
    agent_tentacle_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Empty, not NULL, when the conversation is the agent's own — the same
    # sentinel Thread.channel_thread_id uses, so the plain unique constraint enforces
    # both halves of the identity (NULLs are distinct in a unique constraint,
    # which would let a second bare (thread, agent) row through).
    subagent_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
        index=True,
        comment=(
            "Empty for the agent's own long-lived conversation in the thread; a "
            "subagent's stable identity for a context spawned by a run — Claude's "
            "agentId, Codex's child thread id, a commission's name. Not "
            "external_id: that is a mutable resumable handle, this is identity."
        ),
    )
    parent_conversation_id: Mapped[UUID7 | None] = mapped_column(
        Uuid,
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment=(
            "The conversation whose run spawned this subagent's context — set iff "
            "subagent_id is. Not derivable from the runs: a commission's parent is "
            "a different agent, and the parent's run row does not exist until that "
            "run finishes. Which parent *turn* drove each child run stays on the "
            "run (parent_run_id); this is the stable whose-context half."
        ),
    )

    name: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment=(
            "What this session goes by, as the runtime running it named it — a "
            "Claude session's own ai-title, revised as the session goes on. NULL "
            "for a runtime that names nothing of its own, which is every driven "
            "session, and a reader then falls back to the thread's name."
        ),
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    permission_mode: Mapped[AgentPermissionMode | None] = mapped_column(
        String,
        nullable=True,
        comment=(
            "Approval posture this conversation's agent works under, in that agent's "
            "own vocabulary — `agent_tentacle_id` says which. NULL is nothing "
            "declared, and the agent's configured default decides. Seeded from the "
            "project when the row is created, and owned by the conversation after."
        ),
    )
    effort: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment=(
            "Reasoning effort this conversation's runs ask for, one of the levels "
            "its agent's route claims for the model. NULL is nothing declared, and "
            "the runtime's own default decides. A run given an effort of its own, "
            "as a summon is, uses that one instead."
        ),
    )
    # Native string array on Postgres; SQLite (tests/dev) has no array type, so
    # store the list as JSON there. The Python value is `list[str]` either way.
    allowed_tools: Mapped[list[str]] = mapped_column(
        ARRAY(String).with_variant(JSON(), "sqlite"),
        nullable=False,
        default=list,
    )

    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="ORM optimistic lock rejecting concurrent conversation updates at flush.",
    )
    revision: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="Opaque token checked and replaced when conversation state is saved; NULL before its first save.",
    )
    usage: Mapped[RunUsage] = mapped_column(
        JSON,
        nullable=False,
        default=RunUsage,
        server_default="{}",
        deferred=True,
        comment="Cumulative RunUsage, including executed tool calls absent from response usage; survives context trimming and replaces the previous total on save.",
    )
    deferred_tool_requests: Mapped[DeferredToolRequests | None] = mapped_column(
        JSON(none_as_null=True),
        nullable=True,
        deferred=True,
        comment="Upstream deferred tool requests waiting for approval or external execution.",
    )

    transcript_file: Mapped[File | None] = relationship("File", lazy="raise")
    # The runs of this conversation's history: the ones it ran, and the ones it was
    # forked with. Deleting the conversation leaves its `conversation_runs` rows to
    # the database's cascade; a run still in any history cannot be deleted.
    runs: Mapped[list[AgentRun]] = relationship(
        "AgentRun",
        secondary="conversation_runs",
        # `id` breaks started_at ties so two reads never disagree on the order; at
        # equal stamps chronology is unknowable, and stability is what is owed.
        order_by="(AgentRun.started_at, AgentRun.id)",
        passive_deletes=True,
        lazy="selectin",
    )
    thread: Mapped[Thread | None] = relationship(
        "Thread",
        back_populates="conversations",
        lazy="raise_on_sql",
    )

    @hybrid_property
    def latest_run(self) -> AgentRun | None:
        """The last run in the ordered history, or None before the first run."""
        return self.runs[-1] if self.runs else None

    @property
    def conversation_id(self) -> str:
        return str(self.id)

    @conversation_id.setter
    def conversation_id(self, value: str) -> None:
        if self.id is not None and value != str(self.id):
            raise ValueError("conversation_id must identify the existing conversation")
        self.id = UUID(value)

    __mapper_args__: ClassVar[MapperArgs] = {"version_id_col": version}
