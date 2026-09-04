from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Header, HTTPException, status

if TYPE_CHECKING:
    from octomate.managers.user import UserManager
    from octomate.mcp.base import KnownBearers
    from octomate.schemas.user import UserProfile


def hook_guard(
    bearers: KnownBearers, agent: str
) -> Callable[[str | None], Awaitable[str]]:
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
    bearer passes, and the dependency yields its owner's username — the principal
    the ledger attributes everything this request writes to.

    Registration is demanded rather than defaulted: a host where no user carries a
    secret would serve a router no human's machine could ever reach, and refusing to
    boot is the only honest answer — the alternative is a dead router nobody notices
    until their sessions stop landing in the ledger.
    """
    if not bearers.users:
        raise RuntimeError(
            f"no registered user carries a secret, but agents.{agent} serves a hook "
            "router that authenticates against them. Have the human run `octomate "
            "configure` on their own machine, put what it prints under "
            f"`users.<name>.secret`, and have them run `octomate {agent} hooks "
            "install`."
        )

    async def verify(
        authorization: Annotated[str | None, Header()] = None,
    ) -> str:
        username = (
            bearers.owner(authorization.removeprefix("Bearer "))
            if authorization is not None and authorization.startswith("Bearer ")
            else None
        )
        if username is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid hook credentials",
            )
        return username

    return verify


def hook_sender(
    users: UserManager,
    runtime: str,
    verify: Callable[[str | None], Awaitable[str]],
) -> Callable[[str], Awaitable[UserProfile]]:
    """The guard's second half, as a FastAPI dependency: the verified principal
    resolved to their own profile on `runtime`'s pseudo-channel — who every
    ledger row this request writes is attributed to.

    Composes over `verify` rather than re-reading the header: FastAPI caches a
    dependency's value per request, so a router-level guard and this one share a
    single bearer check."""

    async def sender(username: str = Depends(verify)) -> UserProfile:
        profile = await users.native_profile(runtime, username)
        if profile is None:
            # The guard already proved the username names a registered user, so
            # a miss is a wiring bug worth an error, never a row written to nobody.
            raise RuntimeError(
                f"verified bearer names {username!r}, but the registry holds no "
                "such user"
            )
        return profile

    return sender
