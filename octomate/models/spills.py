from __future__ import annotations

import uuid
from datetime import UTC, datetime

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import DateTime, LargeBinary, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from uuid_utils.compat import uuid7

from octomate.models.base import Base


class ToolOutputSpill(Base, TransmuterProxiedMixin):
    """One oversized tool return, held out of the model's context until it asks.

    No foreign key to a conversation or run: `handle` is minted by pydantic-ai's
    tool-output-limits capability out of its own run and tool-call ids, which are
    not Octomate's. The row is reachable only through that handle, and only until
    the store's retention window closes over it.
    """

    __tablename__ = "tool_output_spills"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    handle: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )
