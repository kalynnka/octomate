"""The conversations table: one agent's context within a thread."""

from __future__ import annotations

from typing import TYPE_CHECKING

from arcanus.base import TransmuterProxiedMixin
from pydantic import UUID7
from sqlalchemy import ARRAY, JSON, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_utils.compat import uuid7

from octomate.models.base import Base
from octomate.types.permissions import AgentPermissionMode

if TYPE_CHECKING:
    from octomate.models.files import File
    from octomate.models.messages import ModelMessage
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
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    through_message_id: Mapped[UUID7 | None] = mapped_column(
        Uuid,
        ForeignKey("model_messages.id"),
        nullable=True,
        comment=(
            "The run's last message this conversation's history includes; NULL is "
            "the whole run. A conversation that abandons a pending tool call stops "
            "short of it here, rather than deleting a message a fork may resume."
        ),
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

    transcript_file: Mapped[File | None] = relationship("File", lazy="raise")
    # The runs of this conversation's history: the ones it ran, and the ones it was
    # forked with. Deleting either side leaves its `conversation_runs` rows to the
    # database's cascade.
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
    # Read-only flat view of every message in the conversation's history, up to
    # each run's bound.
    messages: Mapped[list[ModelMessage]] = relationship(
        "ModelMessage",
        secondary="join(ConversationRun, AgentRun, ConversationRun.run_id == AgentRun.id)",
        primaryjoin="Conversation.id == ConversationRun.conversation_id",
        secondaryjoin=(
            "and_(AgentRun.id == ModelMessage.run_id, "
            "or_(ConversationRun.through_message_id.is_(None), "
            "ModelMessage.id <= ConversationRun.through_message_id))"
        ),
        order_by="(AgentRun.started_at, AgentRun.id, ModelMessage.id)",
        viewonly=True,
        lazy="selectin",
    )

    @hybrid_property
    def latest_run(self) -> AgentRun | None:
        """The last run in the ordered history, or None before the first run."""
        return self.runs[-1] if self.runs else None
