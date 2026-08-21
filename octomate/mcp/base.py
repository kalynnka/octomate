"""What serving the MCP servers shares: the bearer check in front of every one."""

from __future__ import annotations

from secrets import compare_digest

from fastmcp.server.auth import AccessToken, TokenVerifier
from pydantic import SecretStr


class SharedSecret(TokenVerifier):
    """The deployment's secret as every MCP server's bearer.

    The same credential the hook routers take, for the same reason: reachability is
    not a credential, and these tools send to real channels. Every holder is the one
    principal — the operator's own machines — so a token either is the secret or is
    nothing.
    """

    secret: SecretStr

    def __init__(self, secret: SecretStr) -> None:
        super().__init__()
        self.secret = secret

    async def verify_token(self, token: str) -> AccessToken | None:
        # Constant-time: the comparison is against a secret, and the caller controls
        # how often it can ask.
        if not compare_digest(token, self.secret.get_secret_value()):
            return None
        return AccessToken(token=token, client_id="operator", scopes=[])
