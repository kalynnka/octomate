from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, SecretStr, ValidateAs

from octomate.schemas.user import UserProfile


def profile_from_create(profile: UserProfile.Create) -> UserProfile:
    """Validate creation input, then expose the concrete profile type."""
    if "channel_user_id" not in profile.model_fields_set:
        raise ValueError("channel_user_id is required in a YAML profile")
    return UserProfile().absorb(profile)


ConfigProfile = Annotated[
    UserProfile,
    ValidateAs(UserProfile.Create, profile_from_create),
]


class UserConfig(BaseModel):
    """One registered human, keyed by their stable username in YAML."""

    name: str | None = Field(
        default=None,
        description="The name the agent calls this human; defaults to the "
        "`users:` key.",
    )
    nickname: str | None = Field(
        default=None,
        description="A shorter, casual name for this human.",
    )
    profiles: dict[str, ConfigProfile] = Field(
        default_factory=dict,
        description=(
            "Channel tentacle id → a profile mapping with an explicit "
            "channel_user_id. Unknown fields are forbidden. Reconciliation "
            "makes this the profile's sole ownership authority; fields seed an "
            "unseen account and never overwrite channel observations."
        ),
    )
    secret: SecretStr | None = Field(
        default=None,
        description=(
            "This human's own bearer credential — what the served MCP "
            "endpoints and the hook routers authenticate, and what the served "
            "gateway resolves them from: a native session bearing it speaks "
            "for this user. Every configured credential names a person; the "
            "secret lives on the user's registry row, whose unique column "
            "refuses a shared one at reconciliation. Handing it out is "
            "registration: the user runs `octomate "
            "configure --secret` with it, then `octomate <agent> mcp install`, "
            "and exports it as OCTOMATE__SECRET for the installed hooks."
        ),
    )
