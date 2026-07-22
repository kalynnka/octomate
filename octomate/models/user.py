from __future__ import annotations

import uuid
from datetime import datetime

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_utils.compat import uuid7

from octomate.models.base import Base


class User(Base, TransmuterProxiedMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    handle: Mapped[str] = mapped_column(
        String,
        nullable=False,
        unique=True,
        comment="Stable slug naming this human — the `users:` config key.",
    )
    name: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        comment="Canonical display name the agent uses for this human.",
    )

    profiles: Mapped[list[UserProfile]] = relationship(
        "UserProfile",
        back_populates="user",
        lazy="selectin",
    )


class UserProfile(Base, TransmuterProxiedMixin):
    __tablename__ = "user_profiles"
    __table_args__ = (
        UniqueConstraint(
            "channel_tentacle_id",
            "channel_user_id",
            name="uq_user_profiles_channel_identity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    channel_tentacle_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
        comment=(
            "The channel this identity lives on; with channel_user_id it is the "
            "profile's identity — one row per (channel, platform user) ever seen."
        ),
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="The owning User — the link itself, None until proven.",
    )
    channel_user_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
        comment=(
            "Platform user id (Slack Uxxx, Lark open_id, QQ number). Legacy wire "
            "payloads carry it as `user_id`; the schema's before-validator "
            "migrates that shape."
        ),
    )

    name: Mapped[str] = mapped_column(String, nullable=False, default="")
    nickname: Mapped[str | None] = mapped_column(String, nullable=True)
    gender: Mapped[str | None] = mapped_column(String, nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)

    method: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="How the link was proven (config | code); set iff user_id is.",
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="When the link was proven; set iff user_id is.",
    )

    user: Mapped[User | None] = relationship("User", back_populates="profiles")
