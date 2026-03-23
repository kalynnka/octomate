from __future__ import annotations

from datetime import datetime

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase): ...


class Message(Base, TransmuterProxiedMixin):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    message_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    reply_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("messages.message_id"), nullable=True, index=True
    )
    tentacle_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    thread_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    user: Mapped[str] = mapped_column(String, nullable=False, index=True)
    chat: Mapped[str] = mapped_column(String, nullable=False, index=True)
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    role: Mapped[str] = mapped_column(String, nullable=False, index=True)
    content: Mapped[str] = mapped_column(String, nullable=False)

    replied: Mapped[Message | None] = relationship(
        "Message",
        foreign_keys=[reply_id],
        primaryjoin="Message.reply_id == Message.message_id",
        remote_side="Message.message_id",
        lazy="selectin",
        uselist=False,
    )
