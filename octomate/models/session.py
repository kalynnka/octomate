from __future__ import annotations

import uuid

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from uuid_utils.compat import uuid7

from octomate.models.base import Base


class Session(Base, TransmuterProxiedMixin):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint(
            "chat_type",
            "chat_id",
            "thread_id",
            "user_id",
            name="uq_sessions_session_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)

    chat_type: Mapped[str] = mapped_column(String, nullable=False)
    chat_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(
        String, nullable=False, default="", index=True
    )
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)

    channel_tentacle_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    agent_tentacle_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )

    name: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
