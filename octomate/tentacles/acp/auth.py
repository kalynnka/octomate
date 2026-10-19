"""Authenticate ACP connections before starting a native adapter."""

from typing import Annotated

from fastapi import Depends, Header, HTTPException

from octomate.base import Octomate
from octomate.dependencies import application
from octomate.schemas.user import UserProfile
from octomate.tentacles.acp.client import AcpProvider


async def acp_sender(
    provider: AcpProvider,
    app: Annotated[Octomate, Depends(application)],
    authorization: Annotated[str | None, Header()] = None,
) -> UserProfile:
    """Resolve an ACP-scoped bearer to its owner's native provider profile.

    A router uses this dependency before accepting a WebSocket or spawning an
    adapter. Only the Authorization header supplies a credential; browser cookies
    and query parameters cannot authorize agent execution. The existing API-key
    verifier checks scope, expiry and revocation on each connection attempt.

    The returned profile is transient. Authenticating creates no thread, session
    or ledger entry and grants no authority over another connection's sessions.
    """
    scheme, _, token = (authorization or "").partition(" ")
    username = (
        await app.bearers.owner(token, scope="acp")
        if scheme.lower() == "bearer" and token
        else None
    )
    if username is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid ACP credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    profile = await app.users.native_profile(provider.native_id, username)
    if profile is None:
        raise RuntimeError("Verified ACP bearer has no registered owner")
    return profile
