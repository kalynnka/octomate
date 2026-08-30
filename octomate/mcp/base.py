from __future__ import annotations

from secrets import compare_digest

from fastmcp.server.auth import AccessToken, TokenVerifier
from pydantic import SecretStr

from octomate.config.users import UserConfig


class KnownBearers(TokenVerifier):
    """Every credential this deployment accepts, and who each one speaks for.

    Exactly the registered users' secrets from `users.<name>.secret` — every
    configured credential names a person, and the host holds none of its own.
    One registry for every authenticated surface: the served MCP servers verify
    bearers through it, and the hook routers guard with it, so one credential
    per machine reaches both. These tools send to real channels, which is why
    reachability is not a credential: a token either is a registered user's
    secret or is nothing — a deployment with no registered user serves its
    endpoints locked outright.
    """

    users: dict[str, SecretStr]

    def __init__(self, users: dict[str, UserConfig]) -> None:
        super().__init__()
        self.users = {
            username: user.secret
            for username, user in users.items()
            if user.secret is not None
        }

    def owner(self, token: str) -> str | None:
        """The username `token` speaks for, or None for a stranger.

        Constant-time per candidate: the comparisons are against secrets, and
        the caller controls how often it can ask."""
        for username, secret in self.users.items():
            if compare_digest(token, secret.get_secret_value()):
                return username
        return None

    async def verify_token(self, token: str) -> AccessToken | None:
        principal = self.owner(token)
        if principal is None:
            return None
        # The principal rides the verified token as its client id, which is how
        # a served call downstream learns who it speaks for without trusting a
        # header.
        return AccessToken(token=token, client_id=principal, scopes=[])
