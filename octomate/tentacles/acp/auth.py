"""Authenticate the project ACP endpoint before accepting a connection."""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Header, HTTPException

from octomate.schemas.user import UserProfile
from octomate.tentacles.acp.channel import AcpChannelTentacle


def acp_sender(
    tentacle: AcpChannelTentacle,
) -> Callable[[str | None], Awaitable[UserProfile]]:
    """Bind the ACP bearer dependency to the tentacle mounted by the router.

    A router uses this dependency before accepting a WebSocket.
    Only the Authorization header supplies a credential; browser cookies
    and query parameters cannot authorize agent execution. The existing API-key
    verifier checks scope, expiry and revocation on each connection attempt.

    The returned profile is transient. Authenticating creates no thread, session
    or ledger entry and grants no authority over another connection's sessions.
    """

    async def sender(
        authorization: Annotated[str | None, Header()] = None,
    ) -> UserProfile:
        app = tentacle.octomate
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
        profile = await app.users.native_profile(tentacle.id, username)
        if profile is None:
            raise RuntimeError("Verified ACP bearer has no registered owner")
        return profile

    return sender
