"""Per-user OAuth integrations: where a configured `type:` becomes a provider.

An integration is the composition of three things that otherwise know nothing about
each other — a provider's OAuth flow from `octomate.oauth`, a callback transport from
`octomate.schemas.oauth`, and a capability from `octomate.capabilities` — bound to a
connector id and registered on the project's `OAuthManager`.

Everything below that composition is provider-neutral, so this package is the only
place a vendor is named at wiring time. `base` holds the resolution itself.
"""

from octomate.capabilities.github import GitHubCapability
from octomate.capabilities.linear import LinearCapability
from octomate.integrations.base import build_integration

# Ordered by arrival, not alphabetically — the comment below is about that order.
__all__ = [  # noqa: RUF022
    "build_integration",
    # The integrations this deployment can compose, in the order they arrived: a
    # device flow that needs no callback, and an authorization-code flow that does.
    "GitHubCapability",
    "LinearCapability",
]
