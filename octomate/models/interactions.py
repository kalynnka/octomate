from __future__ import annotations

from datetime import datetime

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from octomate.models.base import Base


class Todo(Base, TransmuterProxiedMixin):
    __tablename__ = "todos"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    todo_id: Mapped[str] = mapped_column(
        String, nullable=False, unique=True, index=True
    )
    thread_id: Mapped[str] = mapped_column(
        String, ForeignKey("threads.id"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    active_form: Mapped[str | None] = mapped_column(String, nullable=True)
    assignee: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Question(Base, TransmuterProxiedMixin):
    __tablename__ = "questions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    question_id: Mapped[str] = mapped_column(
        String, nullable=False, unique=True, index=True
    )
    thread_id: Mapped[str] = mapped_column(
        String, ForeignKey("threads.id"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(String, nullable=False)
    options: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    answer: Mapped[str | None] = mapped_column(String, nullable=True)
    responder_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Confirmation(Base, TransmuterProxiedMixin):
    __tablename__ = "confirmations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    confirmation_id: Mapped[str] = mapped_column(
        String, nullable=False, unique=True, index=True
    )
    thread_id: Mapped[str] = mapped_column(
        String, ForeignKey("threads.id"), nullable=False, index=True
    )
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String, nullable=False)
    args: Mapped[dict] = mapped_column(JSON, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    skill: Mapped[str] = mapped_column(String, nullable=False)
    approvers: Mapped[list] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
