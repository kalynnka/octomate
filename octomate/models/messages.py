from __future__ import annotations

from datetime import datetime

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase): ...


class Message(Base, TransmuterProxiedMixin):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tentacle_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    thread_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    user: Mapped[str] = mapped_column(String, nullable=False, index=True)
    chat: Mapped[str] = mapped_column(String, nullable=False, index=True)
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    role: Mapped[str] = mapped_column(String, nullable=False, index=True)
    content: Mapped[str] = mapped_column(String, nullable=False)
