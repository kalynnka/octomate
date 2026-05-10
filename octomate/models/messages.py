from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from arcanus.base import TransmuterProxiedMixin
from pydantic_core import to_jsonable_python
from sqlalchemy import JSON, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator
from uuid_utils.compat import uuid7

from octomate.models.base import Base

if TYPE_CHECKING:
    from octomate.models.runs import AgentRun


class PydanticJSON(TypeDecorator):
    """JSON column whose binds are normalized via pydantic so that
    datetime / dataclass / enum values inside the payload survive
    ``json.dumps`` without a custom engine-level ``json_serializer``.
    """

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        return None if value is None else to_jsonable_python(value)


class ModelMessage(Base, TransmuterProxiedMixin):
    __tablename__ = "model_messages"
    __mapper_args__: ClassVar[dict[str, Any]] = {
        "polymorphic_on": "kind",
        "polymorphic_abstract": True,
    }

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    run_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    kind: Mapped[str] = mapped_column(String, nullable=False, index=True)
    parts: Mapped[list[dict[str, Any]]] = mapped_column(PydanticJSON, nullable=False)
    timestamp: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    # `metadata` is reserved by SQLAlchemy's DeclarativeBase for the table
    # MetaData; expose the column as `meta` on Python and `metadata` in the DB.
    meta: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", PydanticJSON, nullable=True
    )

    instructions: Mapped[str | None] = mapped_column(String, nullable=True)

    usage: Mapped[dict[str, Any] | None] = mapped_column(PydanticJSON, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    provider_name: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    provider_url: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_details: Mapped[dict[str, Any] | None] = mapped_column(
        PydanticJSON, nullable=True
    )
    provider_response_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
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

    run: Mapped[AgentRun] = relationship("AgentRun", back_populates="messages")


class ModelRequest(ModelMessage):
    __mapper_args__: ClassVar[dict[str, Any]] = {"polymorphic_identity": "request"}


class ModelResponse(ModelMessage):
    __mapper_args__: ClassVar[dict[str, Any]] = {"polymorphic_identity": "response"}
