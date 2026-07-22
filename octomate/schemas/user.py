from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from arcanus import BaseTransmuter, RelationCollection, Relationships
from arcanus.base import Identity
from pydantic import ConfigDict, Field, model_validator
from uuid_utils.compat import uuid7

from octomate.models.user import User as UserModel
from octomate.models.user import UserProfile as UserProfileModel
from octomate.schemas.base import sqlalchemy_materia

# How a profile's link to its owning user was proven. Solution C (claim-then-
# confirm) adds "confirm" when it lands.
UserLinkMethod = Literal["config", "code"]

# Placeholder channel_user_id for a profile with no real channel identity — an
# agent's display shim, an unenriched default. "0" is the value the field has
# always defaulted to; no platform issues it as a real id.
ANONYMOUS_CHANNEL_USER_ID = "0"


@sqlalchemy_materia.bless(UserProfileModel)
class UserProfile(BaseTransmuter):
    """A user's identity in one channel — and the boundary parse model for
    platform profile payloads, one class for both lives.

    Ephemeral instances (``MessageEvent.sender``, a tentacle's own ``profile``,
    the per-tentacle fetch cache) are plain unpersisted objects and are shared —
    treat them as read-only. Only ``UserManager``'s registry rows are
    session-bound and mutated; the manager stamps ``channel_tentacle_id`` when
    it persists one.
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
            "The owning User — the link itself, None until proven via config or "
            "a completed handshake."
        ),
    )
    method: UserLinkMethod | None = Field(
        default=None,
        description="How the link was proven; set iff user_id is.",
    )
    verified_at: datetime | None = Field(
        default=None,
        description="When the link was proven; set iff user_id is.",
    )

    @model_validator(mode="before")
    @classmethod
    def claim_legacy_user_id(cls, data: object) -> object:
        """Legacy wire shape: platform payloads and pre-registry sender blobs
        carry the platform id under `user_id` — the name the registry gives the
        owner FK. A dict without `channel_user_id` is that legacy shape: its
        `user_id` is the platform string, never the link."""
        if (
            isinstance(data, dict)
            and "channel_user_id" not in data
            and "user_id" in data
        ):
            data = dict(data)
            data["channel_user_id"] = data.pop("user_id")
        return data



@sqlalchemy_materia.bless(UserModel)
class User(BaseTransmuter):
    """A human across channels; `profiles` are their per-channel identities."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    handle: str = Field(
        frozen=True,
        description="Stable slug naming this human — the `users:` config key.",
    )
    name: str = Field(
        default="",
        description="Canonical display name the agent uses for this human.",
    )

    profiles: RelationCollection[UserProfile] = Relationships()
