import uuid
from typing import Annotated, NotRequired

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import AwareDatetime, Field, SecretStr
from typing_extensions import TypedDict

from octomate.database import async_session
from octomate.dependencies import auth_manager, user_manager
from octomate.managers.auth import AuthManager, InvalidCredentials, UsernameUnavailable
from octomate.managers.user import UserManager
from octomate.schemas.auth import SessionTokens, UserApiKey, UserSession
from octomate.schemas.user import User
from octomate.types.auth import ApiKeyScope, NewPassword


async def current_session(
    request: Request,
    manager: Annotated[AuthManager, Depends(auth_manager)],
) -> UserSession:
    token = request.cookies.get("octomate_access")
    if token is not None:
        session = await manager.authenticate_session(SecretStr(token))
        if session is not None:
            return session
    raise HTTPException(status_code=401, detail="Sign in required")


async def current_user(
    current: Annotated[UserSession, Depends(current_session)],
) -> User:
    async with async_session() as session:
        user = await session.get(User, current.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in required")
    return user


def browser_request(request: Request, response: Response) -> None:
    """A custom header makes cookie-authenticated writes require same-origin JS."""
    response.headers["Cache-Control"] = "no-store"
    if (
        request.method not in {"GET", "HEAD", "OPTIONS"}
        and request.headers.get("X-Octomate-Request") != "1"
    ):
        raise HTTPException(status_code=403, detail="Missing X-Octomate-Request header")


def session_cookies(
    response: Response, tokens: SessionTokens | None, manager: AuthManager
) -> None:
    response.headers["Cache-Control"] = "no-store"
    if tokens is None:
        for name in ("octomate_access", "octomate_refresh"):
            response.delete_cookie(
                name,
                path="/api",
                secure=manager.config.cookie_secure,
                httponly=True,
                samesite="strict",
            )
        return
    for name, token, expires in (
        ("octomate_access", tokens.access_token, tokens.access_expires_at),
        ("octomate_refresh", tokens.refresh_token, tokens.refresh_expires_at),
    ):
        response.set_cookie(
            name,
            token.get_secret_value(),
            expires=expires,
            path="/api",
            secure=manager.config.cookie_secure,
            httponly=True,
            samesite="strict",
        )


class LoginBody(TypedDict):
    username: Annotated[str, Field(min_length=1, max_length=100)]
    password: Annotated[SecretStr, Field(min_length=1, max_length=1024)]


class RegistrationBody(TypedDict):
    username: Annotated[str, Field(min_length=1, max_length=100)]
    password: NewPassword
    name: Annotated[str, Field(min_length=1, max_length=100)]
    invitation: SecretStr


class PasswordBody(TypedDict):
    current_password: Annotated[SecretStr, Field(min_length=1, max_length=1024)]
    password: NewPassword


class ApiKeyBody(TypedDict):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    scopes: Annotated[list[ApiKeyScope], Field(min_length=1)]
    expires_at: NotRequired[AwareDatetime | None]


class ApiKeyResponse(TypedDict):
    """Disclose the token at the HTTP boundary; internal issued keys stay redacted."""

    key: UserApiKey
    token: str


auth_router = APIRouter(
    prefix="/api/auth", tags=["auth"], dependencies=[Depends(browser_request)]
)


@auth_router.post("/register", response_model=User)
async def register(
    body: RegistrationBody,
    response: Response,
    manager: Annotated[AuthManager, Depends(auth_manager)],
    users: Annotated[UserManager, Depends(user_manager)],
) -> User:
    try:
        user = await manager.register(**body)
        tokens = await manager.login(body["username"], body["password"])
    except InvalidCredentials as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    except UsernameUnavailable as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    users.cache_user(user)
    session_cookies(response, tokens, manager)
    return user


@auth_router.post("/login", status_code=204, response_model=None)
async def login(
    body: LoginBody,
    response: Response,
    manager: Annotated[AuthManager, Depends(auth_manager)],
) -> None:
    try:
        tokens = await manager.login(**body)
    except InvalidCredentials as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    session_cookies(response, tokens, manager)


@auth_router.post("/refresh", status_code=204, response_model=None)
async def refresh(
    request: Request,
    response: Response,
    manager: Annotated[AuthManager, Depends(auth_manager)],
) -> None:
    token = request.cookies.get("octomate_refresh")
    if token is None:
        raise HTTPException(status_code=401, detail="Sign in required")
    try:
        tokens = await manager.refresh_session(SecretStr(token))
    except InvalidCredentials as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    session_cookies(response, tokens, manager)


@auth_router.post("/logout", status_code=204, response_model=None)
async def logout(
    request: Request,
    response: Response,
    manager: Annotated[AuthManager, Depends(auth_manager)],
) -> None:
    access = request.cookies.get("octomate_access")
    refresh = request.cookies.get("octomate_refresh")
    await manager.logout(
        SecretStr(access) if access is not None else None,
        SecretStr(refresh) if refresh is not None else None,
    )
    session_cookies(response, None, manager)


@auth_router.post("/password", status_code=204, response_model=None)
async def change_password(
    body: PasswordBody,
    response: Response,
    user: Annotated[User, Depends(current_user)],
    manager: Annotated[AuthManager, Depends(auth_manager)],
) -> None:
    try:
        await manager.set_password(user.username, **body)
    except InvalidCredentials as error:
        raise HTTPException(
            status_code=403, detail="Current password is incorrect"
        ) from error
    session_cookies(response, None, manager)


@auth_router.get("/me", response_model=User)
async def me(response: Response, user: Annotated[User, Depends(current_user)]) -> User:
    response.headers["Cache-Control"] = "no-store"
    return user


@auth_router.post("/api-keys", status_code=201, response_model=ApiKeyResponse)
async def create_api_key(
    body: ApiKeyBody,
    user: Annotated[User, Depends(current_user)],
    manager: Annotated[AuthManager, Depends(auth_manager)],
) -> ApiKeyResponse:
    try:
        issued = await manager.create_api_key(user.id, **body)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"key": issued.key, "token": issued.token.get_secret_value()}


@auth_router.get("/api-keys", response_model=list[UserApiKey])
async def list_api_keys(
    user: Annotated[User, Depends(current_user)],
    manager: Annotated[AuthManager, Depends(auth_manager)],
) -> list[UserApiKey]:
    return await manager.list_api_keys(user.id)


@auth_router.delete("/api-keys/{key_id}", status_code=204, response_model=None)
async def revoke_api_key(
    key_id: uuid.UUID,
    user: Annotated[User, Depends(current_user)],
    manager: Annotated[AuthManager, Depends(auth_manager)],
) -> None:
    try:
        await manager.revoke_api_key(user.id, key_id)
    except InvalidCredentials as error:
        raise HTTPException(status_code=404, detail="API key not found") from error
