"""Inbound OAuth protocol for public native harnesses and CLI installations."""

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from mcp.server.auth.provider import construct_redirect_uri
from mcp.shared.auth import (
    OAuthMetadata,
    ProtectedResourceMetadata,
)
from pydantic import (
    UUID7,
    AnyHttpUrl,
    BaseModel,
    Field,
    SecretStr,
    TypeAdapter,
    ValidationError,
)
from typing_extensions import TypedDict

from octomate.auth import browser_request, current_user
from octomate.database import async_session
from octomate.dependencies import auth_manager
from octomate.managers.auth import AuthManager, InvalidCredentials, InvalidOAuthScopes
from octomate.schemas.authorization import (
    OAuthAuthorization,
    OAuthClient,
    OAuthGrant,
    OAuthToken,
)
from octomate.schemas.user import User
from octomate.types.auth import ApiKeyScope

SCOPES = TypeAdapter(list[ApiKeyScope])
NO_STORE = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
}
CONSENT_PAGE = Path(__file__).with_name("authorization.html").read_text()


class Registration(BaseModel):
    # Preserve redirect strings exactly; URL normalization can change their binding.
    redirect_uris: list[str] = Field(min_length=1, max_length=20)
    client_name: str = Field(default="Native client", min_length=1, max_length=200)
    scope: str = "mcp"
    token_endpoint_auth_method: str = "none"
    grant_types: list[str] = ["authorization_code", "refresh_token"]
    response_types: list[str] = ["code"]


class AuthorizationRequest(BaseModel):
    client_id: UUID7
    redirect_uri: str | None = None
    response_type: str
    code_challenge: str
    code_challenge_method: str
    state: SecretStr | None = None
    scope: str | None = None
    resource: str

    def refuse(self, error: str, redirect: str, origin: str) -> RedirectResponse:
        """Return errors only after the caller has validated the redirect binding."""
        return RedirectResponse(
            construct_redirect_uri(
                redirect,
                error=error,
                state=self.state.get_secret_value() if self.state is not None else None,
                iss=f"{origin}/",
            ),
            status_code=302,
            headers=NO_STORE,
        )


class CodeRequest(BaseModel):
    grant_type: Literal["authorization_code"]
    client_id: UUID7
    code: SecretStr
    redirect_uri: str | None = None
    code_verifier: SecretStr = Field(min_length=43, max_length=128)
    resource: str


class RefreshRequest(BaseModel):
    grant_type: Literal["refresh_token"]
    client_id: UUID7
    refresh_token: SecretStr
    scope: str | None = None
    resource: str


TOKEN_REQUEST = TypeAdapter(
    Annotated[CodeRequest | RefreshRequest, Field(discriminator="grant_type")]
)


class RevocationRequest(BaseModel):
    client_id: UUID7
    token: SecretStr
    token_type_hint: str | None = None


class ConsentBody(TypedDict):
    ticket: SecretStr


class DecisionBody(ConsentBody):
    expected_user_id: UUID7
    approve: bool


def issuer(manager: Annotated[AuthManager, Depends(auth_manager)]) -> str:
    if manager.config.oauth_issuer_url is None:
        raise HTTPException(404, "OAuth is not configured")
    return str(manager.config.oauth_issuer_url).rstrip("/")


def resource_scopes(origin: str, resource: str) -> list[ApiKeyScope]:
    if resource == f"{origin}/octomate/mcp":
        return ["mcp"]
    if resource == f"{origin}/api":
        return ["hooks", "acp"]
    return []


def oauth_error(error: str) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=400, headers=NO_STORE)


def valid_redirect(uri: str) -> bool:
    try:
        parsed = urlsplit(uri)
        _ = parsed.port
    except ValueError:
        return False
    if (
        "#" in uri
        or parsed.username is not None
        or parsed.password is not None
        or any(c.isspace() or ord(c) < 32 for c in uri)
        or "\\" in uri
    ):
        return False
    return bool(parsed.hostname) and (
        parsed.scheme == "https"
        or (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        )
    )


def redirect_matches(requested: str, registered: str) -> bool:
    if requested == registered:
        return True
    # Native clients may choose an ephemeral loopback port (RFC 8252 section 7.3).
    if not valid_redirect(requested):
        return False
    actual, expected = urlsplit(requested), urlsplit(registered)
    return (
        actual.scheme == expected.scheme == "http"
        and actual.hostname == expected.hostname
        and actual.hostname in {"127.0.0.1", "::1"}
        and actual.path == expected.path
        and actual.query == expected.query
    )


router = APIRouter(
    prefix="/oauth", tags=["authorization"], dependencies=[Depends(issuer)]
)
discovery_router = APIRouter(tags=["authorization"], dependencies=[Depends(issuer)])


@discovery_router.get(
    "/.well-known/oauth-authorization-server", response_model_exclude_none=True
)
async def metadata(origin: Annotated[str, Depends(issuer)]) -> OAuthMetadata:
    return OAuthMetadata(
        issuer=AnyHttpUrl(origin),
        authorization_endpoint=AnyHttpUrl(f"{origin}/oauth/authorize"),
        token_endpoint=AnyHttpUrl(f"{origin}/oauth/token"),
        registration_endpoint=AnyHttpUrl(f"{origin}/oauth/register"),
        revocation_endpoint=AnyHttpUrl(f"{origin}/oauth/revoke"),
        scopes_supported=["mcp", "hooks", "acp"],
        grant_types_supported=["authorization_code", "refresh_token"],
        token_endpoint_auth_methods_supported=["none"],
        revocation_endpoint_auth_methods_supported=["none"],
        code_challenge_methods_supported=["S256"],
        authorization_response_iss_parameter_supported=True,
    )


@discovery_router.get(
    "/.well-known/oauth-protected-resource/{path:path}",
    response_model_exclude_none=True,
)
async def resource_metadata(
    path: str, origin: Annotated[str, Depends(issuer)]
) -> ProtectedResourceMetadata:
    resource = f"{origin}/{path}"
    scopes = resource_scopes(origin, resource)
    if not scopes:
        raise HTTPException(404, "Unknown OAuth resource")
    return ProtectedResourceMetadata(
        resource=AnyHttpUrl(resource),
        authorization_servers=[AnyHttpUrl(origin)],
        scopes_supported=list(scopes),
    )


@router.post("/register")
async def register(request: Request) -> Response:
    try:
        body = Registration.model_validate_json(await request.body())
        scopes = SCOPES.validate_python(body.scope.split(" "))
    except ValidationError:
        return oauth_error("invalid_client_metadata")
    if (
        body.token_endpoint_auth_method != "none"
        or set(body.grant_types) != {"authorization_code", "refresh_token"}
        or body.response_types != ["code"]
        or not scopes
    ):
        return oauth_error("invalid_client_metadata")
    if not all(valid_redirect(uri) for uri in body.redirect_uris):
        return oauth_error("invalid_redirect_uri")
    client = OAuthClient(
        name=body.client_name,
        redirect_uris=body.redirect_uris,
        scopes=sorted(set(scopes)),
    )
    async with async_session() as session:
        session.add(client)
        await session.commit()
    return JSONResponse(
        {
            "client_id": str(client.id),
            "client_id_issued_at": int(client.created_at.timestamp()),
            "client_name": client.name,
            "redirect_uris": client.redirect_uris,
            "scope": " ".join(client.scopes),
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
        status_code=201,
        headers=NO_STORE,
    )


@router.get("/authorize")
async def authorize(
    request: Request,
    manager: Annotated[AuthManager, Depends(auth_manager)],
    origin: Annotated[str, Depends(issuer)],
) -> Response:
    params = request.query_params
    if any(len(params.getlist(key)) != 1 for key in params):
        return oauth_error("invalid_request")
    try:
        body = AuthorizationRequest.model_validate(dict(params))
    except ValidationError:
        return oauth_error("invalid_request")
    async with async_session() as session:
        client = await session.get(OAuthClient, body.client_id)
    if client is None:
        return oauth_error("unauthorized_client")
    redirect = body.redirect_uri
    if redirect is None and len(client.redirect_uris) == 1:
        redirect = client.redirect_uris[0]
    if redirect is None or not any(
        redirect_matches(redirect, uri) for uri in client.redirect_uris
    ):
        return oauth_error("invalid_request")
    if body.response_type != "code":
        return body.refuse("unsupported_response_type", redirect, origin)
    if (
        body.code_challenge_method != "S256"
        or len(body.code_challenge) != 43
        or any(
            c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for c in body.code_challenge
        )
    ):
        return body.refuse("invalid_request", redirect, origin)
    allowed = resource_scopes(origin, body.resource)
    if not allowed:
        return body.refuse("invalid_target", redirect, origin)
    try:
        scopes = SCOPES.validate_python(
            body.scope.split(" ") if body.scope is not None else client.scopes
        )
    except ValidationError:
        return body.refuse("invalid_scope", redirect, origin)
    if not scopes or not set(scopes) <= set(client.scopes) & set(allowed):
        return body.refuse("invalid_scope", redirect, origin)
    ticket = SecretStr(secrets.token_urlsafe(32))
    pending = OAuthAuthorization(
        client_id=client.id,
        ticket_hash=manager.hash_token(ticket, manager.config.access_token_salt),
        redirect_uri=redirect,
        redirect_uri_provided_explicitly=body.redirect_uri is not None,
        resource=body.resource,
        scopes=sorted(set(scopes)),
        code_challenge=body.code_challenge,
        state=body.state,
        expires_at=datetime.now(UTC) + manager.config.oauth_authorization_lifetime,
    )
    async with async_session() as session:
        session.add(pending)
        await session.commit()
    return RedirectResponse(
        f"{origin}/oauth/consent#ticket={ticket.get_secret_value()}",
        status_code=302,
        headers=NO_STORE,
    )


@router.get("/consent", response_class=HTMLResponse)
async def consent_page() -> HTMLResponse:
    return HTMLResponse(
        CONSENT_PAGE,
        headers={
            **NO_STORE,
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        },
    )


@router.post("/consent/inspect", dependencies=[Depends(browser_request)])
async def inspect_consent(
    body: ConsentBody,
    user: Annotated[User, Depends(current_user)],
    manager: Annotated[AuthManager, Depends(auth_manager)],
) -> Response:
    try:
        pending = await manager.get_oauth_authorization(body["ticket"])
    except InvalidCredentials:
        return oauth_error("invalid_request")
    async with async_session() as session:
        client = await session.get(OAuthClient, pending.client_id)
    if client is None:
        return oauth_error("invalid_request")
    return JSONResponse(
        {
            "client_name": client.name,
            "redirect_uri": pending.redirect_uri,
            "resource": pending.resource,
            "scopes": pending.scopes,
            "user_id": str(user.id),
            "username": user.username,
        },
        headers=NO_STORE,
    )


@router.post("/consent/decide", dependencies=[Depends(browser_request)])
async def decide_consent(
    body: DecisionBody,
    user: Annotated[User, Depends(current_user)],
    manager: Annotated[AuthManager, Depends(auth_manager)],
    origin: Annotated[str, Depends(issuer)],
) -> Response:
    if body["expected_user_id"] != user.id:
        raise HTTPException(
            409, "Your signed-in account changed. Review the request again."
        )
    try:
        pending = (
            await manager.approve_oauth_authorization(user.id, body["ticket"])
            if body["approve"]
            else await manager.deny_oauth_authorization(body["ticket"])
        )
    except (InvalidCredentials, InvalidOAuthScopes):
        return oauth_error("invalid_request")
    redirect = construct_redirect_uri(
        pending.redirect_uri,
        code=pending.code.get_secret_value() if pending.code is not None else None,
        error=None if body["approve"] else "access_denied",
        state=pending.state.get_secret_value() if pending.state is not None else None,
        iss=f"{origin}/",
    )
    return JSONResponse({"redirect_uri": redirect}, headers=NO_STORE)


@router.post("/token")
async def token(
    request: Request,
    manager: Annotated[AuthManager, Depends(auth_manager)],
    origin: Annotated[str, Depends(issuer)],
) -> Response:
    form = await request.form()
    if (
        any(len(form.getlist(key)) != 1 for key in form)
        or "client_secret" in form
        or "authorization" in request.headers
    ):
        return oauth_error("invalid_request")
    if form.get("grant_type") not in {"authorization_code", "refresh_token"}:
        return oauth_error("unsupported_grant_type")
    try:
        body = TOKEN_REQUEST.validate_python(dict(form))
    except ValidationError:
        return oauth_error("invalid_request")
    if not resource_scopes(origin, body.resource):
        return oauth_error("invalid_target")
    try:
        if isinstance(body, CodeRequest):
            code_hash = manager.hash_token(body.code, manager.config.access_token_salt)
            async with async_session() as session:
                pending = await session.one_or_none(
                    OAuthAuthorization,
                    expressions=[OAuthAuthorization["code_hash"] == code_hash],
                )
            verifier = body.code_verifier.get_secret_value()
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            if (
                pending is None
                or pending.client_id != body.client_id
                or body.redirect_uri
                != (
                    pending.redirect_uri
                    if pending.redirect_uri_provided_explicitly
                    else None
                )
                or any(
                    c
                    not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
                    for c in verifier
                )
                or not hmac.compare_digest(challenge, pending.code_challenge)
            ):
                return oauth_error("invalid_grant")
            issued = await manager.exchange_oauth_code(
                body.client_id, body.code, resource=body.resource
            )
        else:
            scopes = (
                SCOPES.validate_python(body.scope.split(" "))
                if body.scope is not None
                else None
            )
            issued = await manager.refresh_oauth_token(
                body.client_id,
                body.refresh_token,
                resource=body.resource,
                scopes=scopes,
            )
    except (InvalidOAuthScopes, ValidationError):
        return oauth_error("invalid_scope")
    except InvalidCredentials:
        return oauth_error("invalid_grant")
    if issued.access_token is None or issued.refresh_token is None:
        raise RuntimeError("Issued OAuth token has no secrets")
    return JSONResponse(
        {
            "access_token": issued.access_token.get_secret_value(),
            "refresh_token": issued.refresh_token.get_secret_value(),
            "token_type": "Bearer",
            "expires_in": max(
                0, int((issued.access_expires_at - datetime.now(UTC)).total_seconds())
            ),
            "scope": " ".join(issued.scopes),
        },
        headers=NO_STORE,
    )


@router.post("/revoke")
async def revoke(
    request: Request, manager: Annotated[AuthManager, Depends(auth_manager)]
) -> Response:
    form = await request.form()
    if (
        any(len(form.getlist(key)) != 1 for key in form)
        or "client_secret" in form
        or "authorization" in request.headers
    ):
        return oauth_error("invalid_request")
    try:
        body = RevocationRequest.model_validate(dict(form))
    except ValidationError:
        return oauth_error("invalid_request")
    async with async_session() as session:
        token = await session.one_or_none(
            OAuthToken,
            expressions=[
                (
                    OAuthToken["access_token_hash"]
                    == manager.hash_token(body.token, manager.config.access_token_salt)
                )
                | (
                    OAuthToken["refresh_token_hash"]
                    == manager.hash_token(body.token, manager.config.refresh_token_salt)
                )
            ],
        )
        grant = (
            await session.get(OAuthGrant, token.grant_id) if token is not None else None
        )
    if grant is not None and grant.client_id == body.client_id:
        await manager.revoke_oauth_grant(grant.user_id, grant.id)
    return Response(headers=NO_STORE)
