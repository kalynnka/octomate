from __future__ import annotations

from datetime import datetime

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from octomate.models.base import Base


class Thread(Base, TransmuterProxiedMixin):
    __tablename__ = "threads"
    __table_args__ = (
        UniqueConstraint(
            "tentacle_id",
            "user_id",
            "group_id",
            "thread_id",
            "chat_id",
            name="uq_threads_session_key",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tentacle_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    group_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    thread_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    chat_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    owner_tentacle: Mapped[str] = mapped_column(String, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
