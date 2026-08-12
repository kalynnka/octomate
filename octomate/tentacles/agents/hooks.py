from __future__ import annotations

from collections.abc import Awaitable, Callable
from secrets import compare_digest
from typing import Annotated

from fastapi import Header, HTTPException, status
from pydantic import SecretStr


def hook_guard(secret: SecretStr) -> Callable[[str | None], Awaitable[None]]:
    """A bearer check for a hook router, as a FastAPI dependency.

    Hook events are the human ledger — a session's prompts and answers — and the routers
    that take them write straight into thread history, which agents read back. So the
    routers authenticate rather than trust the caller's reach: the transport is plain
    HTTP by design (a client may be a different machine than Octomate), and reachability
    is not a credential.

    Takes the secret rather than reading an environment variable: the running app knows
    this as `OctomateConfig.hook_secret`, and where that came from — `octomate.yaml`, the
    environment, `.env` — is the config's business and not this module's. How a *client*
    is told to carry it is the installer's business (`octomate.cli.hooks`).
    """
    expected = f"Bearer {secret.get_secret_value()}"

    async def verify(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        # Constant-time: the comparison is against a secret, and the caller controls how
        # often it can ask.
        if authorization is None or not compare_digest(authorization, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid hook credentials",
            )

    return verify
