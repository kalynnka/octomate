"""Tentacles — channel adapters that connect external services to the Nerve."""

from octomate.tentacles.base import BaseTentacle
from octomate.tentacles.napcat import NapcatTentacle

__all__ = [
    "BaseTentacle",
    "NapcatTentacle",
]
