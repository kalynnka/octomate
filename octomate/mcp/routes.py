import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from octomate.auth import browser_request, current_user
from octomate.dependencies import mcp_manager
from octomate.managers.mcp import McpManager, McpUnavailable
from octomate.schemas.mcp import (
    Mcp,
    McpInstallRequest,
    McpVariant,
)
from octomate.schemas.user import User

mcp_router = APIRouter(
    prefix="/api/mcp/instances",
    tags=["mcp"],
    dependencies=[Depends(browser_request)],
)


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
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


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
