"""The browser half of an authorization-code connection.

Two routes, and deliberately only two: one that turns a UUID into the provider's
real authorization request, and one the provider sends the user back to. Both are
opened by a person's browser rather than called by anything — the authorization
server never connects here, it only redirects the user agent — so what they answer
with is a page for someone to read, and what they refuse with is the same page for
every way of refusing.

Neither route authenticates its caller, because there is nobody to authenticate: the
callback arrives out of band with no channel identity attached. Everything binding
the round trip to one person was settled when the authorization started and is
rechecked in `OAuthManager.complete_callback`.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from octomate.managers.oauth import OAuthManager, UnusableOAuthOperation
from octomate.schemas.oauth import OAUTH_CALLBACK_PATH, OAUTH_START_PATH

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)


def oauth_manager(request: Request) -> OAuthManager:
    """The project's one OAuth manager, off the Octomate instance serving this app.

    Resolved per request rather than closed over, so the router is a plain module
    object and nothing has to be threaded to it. `Octomate.app` puts itself on
    `app.state`, which is the only dynamic step and the only place this narrows.
    """
    octomate: Octomate = request.app.state.octomate
    return octomate.oauth


# The manager every route in this module reads, named once so a test can override it.
ManagedOAuth = Annotated[OAuthManager, Depends(oauth_manager)]


def page(title: str, detail: str, *, status_code: int = 200) -> HTMLResponse:
    """The whole user-facing surface of this router.

    Says what happened and where the rest of the errand is, and nothing else — no
    provider name in an error, no operation id, no reason. Someone who reaches a
    refusal here is either the user, who needs to go back and ask again, or someone
    who should learn nothing from having tried.
    """
    return HTMLResponse(
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title}</title>"
        "<style>body{font:16px/1.6 system-ui,sans-serif;margin:0;display:grid;"
        "place-items:center;min-height:100vh;padding:2rem;color:#1c1c1e;"
        "background:#fbfbfd}main{max-width:26rem;text-align:center}"
        "h1{font-size:1.25rem;margin:0 0 .5rem}p{margin:0;color:#5f5f66}"
        "@media(prefers-color-scheme:dark){body{color:#f2f2f7;background:#131315}"
        "p{color:#a1a1aa}}</style></head>"
        f"<body><main><h1>{title}</h1><p>{detail}</p></main></body></html>",
        status_code=status_code,
    )


oauth_router = APIRouter(tags=["oauth"])


@oauth_router.get(OAUTH_START_PATH, include_in_schema=False)
async def start(
    connector_id: str,
    operation_id: uuid.UUID,
    manager: ManagedOAuth,
) -> Response:
    try:
        staged = await manager.staged_authorization(connector_id, operation_id)
    except UnusableOAuthOperation as error:
        logger.info("Refused an OAuth start link: %s", error)
        return page(
            "This link has expired",
            "Ask again in the chat where you requested it and a fresh one will arrive.",
            status_code=404,
        )
    # 307 rather than 302: a browser that turned this into a GET would be harmless
    # today, but the provider request is this operation's only chance to be made
    # exactly as it was staged.
    return RedirectResponse(str(staged.authorization_uri), status_code=307)


@oauth_router.get(OAUTH_CALLBACK_PATH, include_in_schema=False)
async def callback(
    connector_id: str,
    manager: ManagedOAuth,
    state: Annotated[str, Query()] = "",
    code: Annotated[str, Query()] = "",
    error: Annotated[str, Query()] = "",
) -> HTMLResponse:
    if not state:
        return page(
            "Something went wrong",
            "This page was opened without an authorization to finish.",
            status_code=400,
        )
    if error or not code:
        # A provider that says no still says which authorization it is saying no
        # about, so the operation is closed rather than left live until it ages out.
        # Failing to close it is not worth telling the user about.
        try:
            await manager.abandon_callback(connector_id, state=state)
        except UnusableOAuthOperation as unusable:
            logger.info("Could not close a declined authorization: %s", unusable)
        return page(
            "Not connected",
            "The authorization was declined. You can ask again in the chat any time.",
        )
    try:
        grant = await manager.complete_callback(connector_id, state=state, code=code)
    except UnusableOAuthOperation as unusable:
        logger.info("Refused an OAuth callback: %s", unusable)
        return page(
            "This link has expired",
            "Ask again in the chat where you requested it and a fresh one will arrive.",
            status_code=404,
        )
    except Exception:
        # The provider refused the exchange, or never answered. The operation is
        # spent either way, so there is nothing to do but say so and let them ask
        # for another.
        logger.exception("Failed to complete an OAuth callback")
        return page(
            "Something went wrong",
            "The connection could not be completed. Ask again in the chat and a "
            "fresh link will arrive.",
            status_code=502,
        )
    return page(
        f"Connected as {grant.account_label}",
        "You can close this tab and go back to the chat.",
    )
