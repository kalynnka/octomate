import logging
import uuid
from typing import Annotated

import httpx2
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from mcp.client.auth.exceptions import OAuthFlowError

from octomate.auth import browser_request, current_user
from octomate.dependencies import mcp_manager
from octomate.managers.mcp import McpManager, McpUnavailable
from octomate.schemas.mcp import (
    Mcp,
    McpAuthorizationResult,
    McpInstallRequest,
    McpTentacleInfo,
    McpVariant,
)
from octomate.schemas.oauth import DeviceAuthorization, OAuthStartResult
from octomate.schemas.user import User

logger = logging.getLogger(__name__)

mcp_router = APIRouter(
    prefix="/api/mcp",
    tags=["mcp"],
    dependencies=[Depends(browser_request)],
)


@mcp_router.get("/tentacles", response_model=list[McpTentacleInfo])
async def available_tentacles(
    user: Annotated[User, Depends(current_user)],
    manager: Annotated[McpManager, Depends(mcp_manager)],
) -> list[McpTentacleInfo]:
    return manager.available()


@mcp_router.get("", response_model=list[McpVariant])
async def list_instances(
    user: Annotated[User, Depends(current_user)],
    manager: Annotated[McpManager, Depends(mcp_manager)],
) -> list[Mcp]:
    return await manager.list(user.id)


@mcp_router.post("", status_code=201, response_model=McpVariant)
async def install(
    body: McpInstallRequest,
    user: Annotated[User, Depends(current_user)],
    manager: Annotated[McpManager, Depends(mcp_manager)],
) -> Mcp:
    try:
        return await manager.install(user.id, body)
    except McpUnavailable as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@mcp_router.post("/{mcp_id}/connect", response_model=OAuthStartResult)
async def connect(
    mcp_id: uuid.UUID,
    user: Annotated[User, Depends(current_user)],
    manager: Annotated[McpManager, Depends(mcp_manager)],
) -> OAuthStartResult | JSONResponse:
    try:
        authorization = await manager.connect(user, mcp_id)
    except McpUnavailable as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (ValueError, httpx2.HTTPError, OAuthFlowError) as error:
        logger.warning("Failed to authorize MCP %s (%s)", mcp_id, type(error).__name__)
        raise HTTPException(
            status_code=400, detail="MCP authorization could not be started"
        ) from error
    if isinstance(authorization, DeviceAuthorization):
        return JSONResponse(
            content=authorization.model_dump(mode="json")
            | {"user_code": authorization.user_code.get_secret_value()},
            headers={"Cache-Control": "no-store"},
        )
    return authorization


@mcp_router.post("/{mcp_id}/confirm", response_model=McpAuthorizationResult)
async def confirm(
    mcp_id: uuid.UUID,
    user: Annotated[User, Depends(current_user)],
    manager: Annotated[McpManager, Depends(mcp_manager)],
) -> McpAuthorizationResult:
    try:
        return await manager.confirm(user, mcp_id)
    except McpUnavailable as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (ValueError, httpx2.HTTPError, OAuthFlowError) as error:
        logger.warning("Failed to confirm MCP %s (%s)", mcp_id, type(error).__name__)
        raise HTTPException(
            status_code=400, detail="MCP authorization could not be confirmed"
        ) from error


@mcp_router.post("/{mcp_id}/enable", response_model=McpVariant)
async def enable(
    mcp_id: uuid.UUID,
    user: Annotated[User, Depends(current_user)],
    manager: Annotated[McpManager, Depends(mcp_manager)],
) -> Mcp:
    try:
        return await manager.enable(user_id=user.id, mcp_id=mcp_id)
    except McpUnavailable as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@mcp_router.post("/{mcp_id}/disable", response_model=McpVariant)
async def disable(
    mcp_id: uuid.UUID,
    user: Annotated[User, Depends(current_user)],
    manager: Annotated[McpManager, Depends(mcp_manager)],
) -> Mcp:
    try:
        return await manager.disable(user_id=user.id, mcp_id=mcp_id)
    except McpUnavailable as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@mcp_router.delete("/{mcp_id}", status_code=204)
async def uninstall(
    mcp_id: uuid.UUID,
    user: Annotated[User, Depends(current_user)],
    manager: Annotated[McpManager, Depends(mcp_manager)],
) -> None:
    try:
        await manager.uninstall(user.id, mcp_id)
    except McpUnavailable as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
