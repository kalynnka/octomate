from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class GitIdentity(BaseModel):
    """The identity on commits Octomate itself makes — a directory mirror's sync
    commits today, thread-branch turn commits later. The machine's by default, so
    its record-keeping is never attributed to whoever's dotfiles are installed,
    and a fresh server commits without a `git config --global` first."""

    name: str = "octomate"
    # example.com because it is the domain RFC 2606 reserves for exactly this:
    # a real-looking address that is guaranteed to belong to nobody.
    email: EmailStr = "octomate@example.com"


class MirrorsConfig(BaseModel):
    """How project mirrors are synced."""

    freshness_window: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Seconds within which a synced mirror is current enough to fork "
            "without asking the upstream again. Zero on purpose: a failed fetch "
            "already degrades to the stale mirror, so this is purely a "
            "performance knob, and there is no number to set it to before the "
            "fetch cost is measured where the server actually runs."
        ),
    )
    identity: GitIdentity = Field(
        default_factory=GitIdentity,
        description="The identity on commits Octomate itself makes.",
    )
