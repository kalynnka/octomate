from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated

from fastapi import Header, HTTPException, status

if TYPE_CHECKING:
    from octomate.mcp.base import KnownBearers


def hook_guard(
    bearers: KnownBearers, agent: str
) -> Callable[[str | None], Awaitable[None]]:
    """A bearer check for `agent`'s hook router, as a FastAPI dependency.

    Hook events are the human ledger — a session's prompts and answers — and the routers
    that take them write straight into thread history, which agents read back. So the
    routers authenticate rather than trust the caller's reach: the transport is plain
    HTTP by design (a client may be a different machine than Octomate), and reachability
    is not a credential.

    Takes the bearer registry rather than reading an environment variable: the running
    app builds it as `Octomate.bearers()`, and where its credentials came from — the
    users' YAML entries — is the config's business and not this module's. How a *client*
    is told to carry one is the installer's business (`octomate_cli.hooks`). Any known
    bearer passes, though the rows a hook writes still carry the shared native
    identity, whoever bore the token.

    Registration is demanded rather than defaulted: a host where no user carries a
    secret would serve a router no human's machine could ever reach, and refusing to
    boot is the only honest answer — the alternative is a dead router nobody notices
    until their sessions stop landing in the ledger.
    """
    if not bearers.users:
        raise RuntimeError(
            f"no registered user carries a secret, but agents.{agent} serves a hook "
            "router that authenticates against them. Add one under `users.<name>."
            "secret` (`octomate secret` mints one), hand it to that human, and have "
            f"them re-run `octomate {agent} hooks install`."
        )

    async def verify(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        if (
            authorization is None
            or not authorization.startswith("Bearer ")
            or bearers.owner(authorization.removeprefix("Bearer ")) is None
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid hook credentials",
            )

    return verify
