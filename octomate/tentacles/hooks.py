from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Header, HTTPException, status

if TYPE_CHECKING:
    from octomate.managers.user import UserManager
    from octomate.mcp.base import KnownBearers
    from octomate.schemas.user import UserProfile


def hook_guard(
    bearers: KnownBearers,
) -> Callable[[str | None], Awaitable[str]]:
    """Authenticate a hook request against API tokens with the hooks scope.

    The dependency yields the owner's username for ledger attribution.
    """

    async def verify(
        authorization: Annotated[str | None, Header()] = None,
    ) -> str:
        username = (
            await bearers.owner(authorization.removeprefix("Bearer "), scope="hooks")
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
