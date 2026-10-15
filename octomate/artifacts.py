"""Authorized artifact reads, mounted by the host independently of any channel.

GET `/api/artifacts/{artifact_id}` reads an owned artifact without requiring a
thread. GET `/api/trunkline/threads/{thread_id}/artifacts/{message_id}` additionally
requires thread access and a visible ledger reference, including fork cutoffs.

Both return the saved invocation, a freshly fetched UI resource, and the configured
sandbox origin with Cache-Control: no-store. No provider credentials are returned;
there is no caller-selected resource URI or tool execution endpoint. HTML stays
untrusted JSON data here; the viewer and sandbox are implemented separately.
"""

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Annotated

import httpx2
from fastapi import APIRouter, Depends, HTTPException, Request
from mcp.shared.exceptions import MCPError
from pydantic import UUID7, AnyHttpUrl

from octomate.auth import browser_request, current_user
from octomate.dependencies import artifact_manager
from octomate.managers.artifacts import (
    ArtifactManager,
    ArtifactSandboxUnavailable,
    ArtifactUnavailable,
)
from octomate.schemas.artifacts import McpAppView
from octomate.schemas.user import User

logger = logging.getLogger(__name__)
NO_STORE = {"Cache-Control": "no-store"}

artifact_router = APIRouter(tags=["artifacts"], dependencies=[Depends(browser_request)])


@contextmanager
def artifact_errors() -> Generator[None]:
    """Translate artifact loading failures into safe, uncached HTTP responses.

    Unknown or inaccessible artifacts/installations return 404, missing or
    same-origin sandbox configuration returns 503, and invalid or failed upstream
    resource reads return 502. Upstream exception details stay server-side.
    """
    try:
        yield
    except ArtifactUnavailable as error:
        raise HTTPException(404, str(error), headers=NO_STORE) from error
    except ArtifactSandboxUnavailable as error:
        raise HTTPException(503, str(error), headers=NO_STORE) from error
    except (ValueError, httpx2.HTTPError, MCPError, TimeoutError) as error:
        logger.warning("Failed to load artifact (%s)", type(error).__name__)
        raise HTTPException(
            502, "MCP app resource could not be loaded", headers=NO_STORE
        ) from error


@artifact_router.get("/api/artifacts/{artifact_id}", response_model=McpAppView)
async def read_artifact(
    artifact_id: UUID7,
    user: Annotated[User, Depends(current_user)],
    manager: Annotated[ArtifactManager, Depends(artifact_manager)],
    request: Request,
) -> McpAppView:
    """Read owned content independently of whether any thread still references it."""
    with artifact_errors():
        return await manager.load(
            artifact_id, user, console_origin=AnyHttpUrl(str(request.base_url))
        )


@artifact_router.get(
    "/api/trunkline/threads/{thread_id}/artifacts/{message_id}",
    response_model=McpAppView,
)
async def read_thread_artifact(
    thread_id: UUID7,
    message_id: UUID7,
    user: Annotated[User, Depends(current_user)],
    manager: Annotated[ArtifactManager, Depends(artifact_manager)],
    request: Request,
) -> McpAppView:
    """Read an owned artifact only through a reference visible in this thread."""
    with artifact_errors():
        return await manager.load_from_message(
            thread_id,
            message_id,
            user,
            console_origin=AnyHttpUrl(str(request.base_url)),
        )
