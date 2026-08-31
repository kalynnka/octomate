from __future__ import annotations

import uuid

from arcanus.base import TransmuterProxiedMixin
from pydantic import SecretStr
from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_utils.compat import uuid7

from octomate.models.base import Base, SecretString


class User(Base, TransmuterProxiedMixin):
    __tablename__ = "users"
    # Named, not `unique=True`: the metadata has no naming convention, and an
    # unnamed constraint cannot be dropped by a downgrade.
    __table_args__ = (UniqueConstraint("secret", name="uq_users_secret"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    username: Mapped[str] = mapped_column(
        String,
        nullable=False,
        unique=True,
        comment="Stable username naming this human — the `users:` YAML key.",
    )
    name: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        comment="Canonical display name the agent uses for this human.",
    )
    nickname: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="A shorter, casual name for this human.",
    )
    secret: Mapped[SecretStr | None] = mapped_column(
        SecretString,
        nullable=True,
        comment=(
            "This human's own bearer credential, reconciled from `users.<name>."
            "secret` — what the served MCP endpoints and hook routers "
            "authenticate. Unique, so a bearer names exactly one user; NULL for "
            "a user registered only to link accounts. Stored plaintext by the "
            "same posture as the YAML that seeds it, `SecretStr` above the "
            "column."
        ),
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
        comment="The YAML-declared owning User; NULL for a visitor profile.",
    )
    channel_user_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
        comment="Platform user id (Slack Uxxx, Lark open_id, QQ number).",
    )

    name: Mapped[str] = mapped_column(String, nullable=False, default="")
    nickname: Mapped[str | None] = mapped_column(String, nullable=True)
    gender: Mapped[str | None] = mapped_column(String, nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)

    user: Mapped[User | None] = relationship(
        "User",
        back_populates="profiles",
        lazy="selectin",
    )
