from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field

from octomate.schemas.user import UserProfile


def profile_from_platform_id(value: object) -> object:
    """A terse `channel: platform-id` string declares just the identity."""
    if isinstance(value, str):
        return {"channel_user_id": value}
    return value


ConfigProfile = Annotated[UserProfile, BeforeValidator(profile_from_platform_id)]


class UserConfig(BaseModel):
    """One human, keyed in the `users:` section by their stable handle."""

    name: str = Field(
        default="",
        description="Canonical display name the agent uses for this human.",
    )
    profiles: dict[str, ConfigProfile] = Field(
        default_factory=dict,
        description=(
            "Channel tentacle id → the user's profile on that channel: a bare "
            "platform user id (Slack Uxxx, Lark open_id, QQ number) or a profile "
            'mapping. Reconciled at startup as method="config" links; profile '
            "fields seed a never-seen account and never overwrite observations."
        ),
    )
