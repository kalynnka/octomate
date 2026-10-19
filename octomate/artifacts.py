"""Authorized artifact reads, mounted by the host independently of any channel.

GET `/api/artifacts/{artifact_id}` reads an owned artifact without requiring a
thread. GET `/api/trunkline/threads/{thread_id}/artifacts/{message_id}` additionally
requires thread access and a visible ledger reference, including fork cutoffs.

Both return captured HTML or a saved invocation with its freshly fetched UI resource,
and the configured sandbox origin with Cache-Control: no-store. No provider credentials are returned;
there is no caller-selected resource URI or tool execution endpoint. HTML stays
untrusted JSON data here. The SPA viewer uses the configured artifact prefix;
GET `/api/artifacts/config` supplies that public routing configuration. The
isolated runtime is served on its configured origin through the same listener.

The proxy's response CSP bounds the inner document's inherited policy. A second,
stricter CSP inside srcdoc removes the proxy's same-origin frame allowance. The
inner iframe grants scripts only, so it has an opaque origin and cannot navigate
the console, open popups, submit forms, or download files. No browser permissions
are granted. The policy URL is public and contains no artifact or invocation data.
"""

import logging
from collections.abc import Generator
from contextlib import contextmanager
from html import escape
from pathlib import Path
from typing import Annotated

import httpx2
from fastapi import APIRouter, Depends, HTTPException, Request
from mcp.shared.exceptions import MCPError
from pydantic import UUID7, AnyHttpUrl, ValidationError
from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.responses import FileResponse, HTMLResponse, Response
from starlette.routing import Route, Router
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from octomate.auth import browser_request, current_user
from octomate.config.artifacts import ArtifactConfig
from octomate.dependencies import artifact_manager
from octomate.managers.artifacts import (
    ArtifactManager,
    ArtifactSandboxUnavailable,
    ArtifactUnavailable,
)
from octomate.schemas.artifacts import ArtifactSandboxPolicy, HtmlView, McpAppView
from octomate.schemas.user import User

logger = logging.getLogger(__name__)
NO_STORE = {"Cache-Control": "no-store"}
ASSETS = Path(__file__).parent / "static" / "artifacts"
PROXY_HTML = (ASSETS / "sandbox.html").read_text()
PERMISSIONS = (
    "camera=(), microphone=(), geolocation=(), clipboard-read=(), "
    "clipboard-write=(), fullscreen=(), display-capture=(), payment=(), usb=()"
)


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


@artifact_router.get("/api/artifacts/config", response_model=ArtifactConfig)
async def artifact_config(
    manager: Annotated[ArtifactManager, Depends(artifact_manager)],
) -> ArtifactConfig:
    """Public viewer routing settings; provider credentials are never exposed."""
    return manager.config


@artifact_router.get(
    "/api/artifacts/{artifact_id}",
    response_model=McpAppView | HtmlView,
    response_model_exclude_none=True,
)
async def read_artifact(
    artifact_id: UUID7,
    user: Annotated[User, Depends(current_user)],
    manager: Annotated[ArtifactManager, Depends(artifact_manager)],
    request: Request,
) -> McpAppView | HtmlView:
    """Read owned content independently of whether any thread still references it."""
    with artifact_errors():
        return await manager.load(
            artifact_id, user, console_origin=AnyHttpUrl(str(request.base_url))
        )


@artifact_router.get(
    "/api/trunkline/threads/{thread_id}/artifacts/{message_id}",
    response_model=McpAppView | HtmlView,
    response_model_exclude_none=True,
)
async def read_thread_artifact(
    thread_id: UUID7,
    message_id: UUID7,
    user: Annotated[User, Depends(current_user)],
    manager: Annotated[ArtifactManager, Depends(artifact_manager)],
    request: Request,
) -> McpAppView | HtmlView:
    """Read an owned artifact only through a reference visible in this thread."""
    with artifact_errors():
        return await manager.load_from_message(
            thread_id,
            message_id,
            user,
            console_origin=AnyHttpUrl(str(request.base_url)),
        )


def sandbox_csp(policy: ArtifactSandboxPolicy, *, proxy: bool) -> str:
    """Construct restrictive source lists; only the outer proxy may frame itself."""
    csp = policy.csp
    resources = " ".join(csp.resource_domains or [])
    frames = " ".join((["'self'"] if proxy else []) + (csp.frame_domains or []))
    directives = [
        "default-src 'none'",
        f"script-src 'self' 'unsafe-inline' {resources}",
        f"style-src 'self' 'unsafe-inline' {resources}",
        f"img-src 'self' data: {resources}",
        f"font-src 'self' data: {resources}",
        f"media-src 'self' data: {resources}",
        "connect-src " + (" ".join(csp.connect_domains or []) or "'none'"),
        "frame-src " + (frames or "'none'"),
        "base-uri " + (" ".join(csp.base_uri_domains or []) or "'none'"),
        "object-src 'none'",
        "form-action 'none'",
        "worker-src 'none'",
    ]
    if proxy:
        directives.append(
            "frame-ancestors " + str(policy.parent_origin).removesuffix("/")
        )
    return "; ".join(directive.rstrip() for directive in directives)


async def sandbox_proxy(request: Request) -> Response:
    """Validate policy before emitting headers; HTML arrives later via postMessage."""
    raw = request.query_params.get("policy", "")
    if len(raw) > 16384:
        return Response(status_code=422)
    try:
        policy = ArtifactSandboxPolicy.model_validate_json(raw)
    except ValidationError:
        return Response(status_code=422)
    if policy.parent_origin == AnyHttpUrl(str(request.base_url)):
        return Response(status_code=422)
    html = (
        PROXY_HTML.replace(
            "{{parent_origin}}",
            escape(str(policy.parent_origin).removesuffix("/"), quote=True),
        )
        .replace("{{csp}}", escape(sandbox_csp(policy, proxy=False), quote=True))
        .replace("{{content_kind}}", policy.content_kind)
    )
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": sandbox_csp(policy, proxy=True),
            "Permissions-Policy": PERMISSIONS,
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def sandbox_script(request: Request) -> Response:
    """The proxy's bundled script, with no console assets on this origin."""
    return FileResponse(
        ASSETS / "sandbox.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


class ArtifactSandbox:
    """Dispatch the configured sandbox origin before any console or account route.

    Reverse proxies must preserve Host and the external scheme. Requests on this
    origin cannot reach Octomate APIs, OpenAPI, cookies, WebSockets, or SPA assets.
    The sandbox paths are unavailable on all other origins.
    """

    def __init__(self, app: ASGIApp, *, origin: AnyHttpUrl | None) -> None:
        self.app: ASGIApp = app
        self.origin: AnyHttpUrl | None = origin
        self.router: ASGIApp = ExceptionMiddleware(
            Router(
                routes=[
                    Route("/artifacts/sandbox", sandbox_proxy),
                    Route("/artifacts/sandbox.js", sandbox_script),
                ]
            )
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        request = Request({**scope, "type": "http"})
        url = AnyHttpUrl(
            str(request.base_url)
            .replace("ws://", "http://")
            .replace("wss://", "https://")
        )
        isolated = (
            self.origin is not None
            and url.host == self.origin.host
            and (
                url.port == self.origin.port
                or (url.port in {80, 443} and self.origin.port in {80, 443})
            )
        )
        if (
            scope["type"] == "http"
            and isolated
            and self.origin is not None
            and url.scheme == self.origin.scheme
        ):
            await self.router(scope, receive, send)
        elif isolated or scope["path"] in {
            "/artifacts/sandbox",
            "/artifacts/sandbox.js",
        }:
            if scope["type"] == "websocket":
                await WebSocket(scope, receive, send).close()
            else:
                await Response(status_code=404)(scope, receive, send)
        else:
            await self.app(scope, receive, send)
