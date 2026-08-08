from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import ARRAY, JSON, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_utils.compat import uuid7

from octomate.models.base import Base

if TYPE_CHECKING:
    from octomate.models.messages import ModelMessage
    from octomate.models.runs import AgentRun
    from octomate.models.thread import Thread


class Conversation(Base, TransmuterProxiedMixin):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint(
            "thread_id",
            "agent_tentacle_id",
            "subagent_id",
            name="uq_conversations_conversation_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)

    thread_id: Mapped[uuid.UUID] = mapped_column(
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
    parent_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
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

    name: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    permission_mode: Mapped[str] = mapped_column(
        String, nullable=False, default="default"
    )
    # Native string array on Postgres; SQLite (tests/dev) has no array type, so
    # store the list as JSON there. The Python value is `list[str]` either way.
    allowed_tools: Mapped[list[str]] = mapped_column(
        ARRAY(String).with_variant(JSON(), "sqlite"),
        nullable=False,
        default=list,
    )

    runs: Mapped[list[AgentRun]] = relationship(
        "AgentRun",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AgentRun.started_at",
        lazy="selectin",
    )
    thread: Mapped[Thread | None] = relationship(
        "Thread",
        back_populates="conversations",
    )
    # Read-only flat view of every message in the conversation, joined through
    # agent_runs. Writes go through `runs` and each run's `messages`.
    messages: Mapped[list[ModelMessage]] = relationship(
        "ModelMessage",
        secondary="agent_runs",
        primaryjoin="Conversation.id == AgentRun.conversation_id",
        secondaryjoin="AgentRun.id == ModelMessage.run_id",
        order_by="(AgentRun.started_at, ModelMessage.id)",
        viewonly=True,
        lazy="selectin",
    )
