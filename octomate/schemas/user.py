from __future__ import annotations

import uuid
from typing import Annotated

from arcanus import BaseTransmuter, Relation, RelationCollection, Relationships
from arcanus.base import Identity
from pydantic import ConfigDict, Field
from uuid_utils.compat import uuid7

from octomate.models.user import User as UserModel
from octomate.models.user import UserProfile as UserProfileModel
from octomate.schemas.base import sqlalchemy_materia

# Placeholder channel_user_id for a profile with no real channel identity — an
# agent's display shim, an unenriched default. "0" is the value the field has
# always defaulted to; no platform issues it as a real id.
ANONYMOUS_CHANNEL_USER_ID = "0"


@sqlalchemy_materia.bless(UserProfileModel)
class UserProfile(BaseTransmuter):
    """An observed channel identity and the boundary channel-profile shape.

    Persisted profiles belong to a YAML-declared ``User`` or remain ownerless
    visitors. Ephemeral channel snapshots also have no owner; ``UserManager``
    stamps their channel id and resolves ownership from the registry row.
    """

    model_config = ConfigDict(
        validate_by_alias=True,
        validate_by_name=True,
        extra="ignore",
        coerce_numbers_to_str=True,
        from_attributes=True,
    )

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    channel_tentacle_id: str = Field(
        default="",
        description=(
            "The channel this identity lives on; empty on ephemeral boundary "
            "instances, stamped by UserManager at persist time."
        ),
    )
    channel_user_id: str = ANONYMOUS_CHANNEL_USER_ID
    name: str = ""
    nickname: str | None = None
    gender: str | None = None
    age: int | None = None
    title: str | None = None

    user_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "The YAML-declared owning User; None for ephemeral snapshots and "
            "persisted visitor profiles."
        ),
    )
    user: Relation[User | None] = Field(
        default_factory=Relation,
        frozen=True,
        exclude=True,
        description="The optional YAML-declared owner, eagerly loaded.",
    )


@sqlalchemy_materia.bless(UserModel)
class User(BaseTransmuter):
    """A YAML-declared human across channels."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    username: str = Field(
        frozen=True,
        description="Stable username naming this human — the `users:` YAML key.",
    )
    name: str = Field(
        default="",
        description="Canonical display name the agent uses for this human.",
    )
    nickname: str | None = Field(
        default=None,
        description="A shorter, casual name for this human.",
    )

    profiles: RelationCollection[UserProfile] = Relationships()


# `UserProfile.user` names `User`, which is declared below it, so the profile is
# incomplete until this runs. Subclasses inherit that, and a cold import of one —
# napcat's sender — fails to validate rather than to build.
UserProfile.model_rebuild()
