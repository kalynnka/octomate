from pydantic import SecretStr

from octomate.config import AuthConfig
from octomate.database import async_session
from octomate.managers.auth import AuthManager
from octomate.schemas.auth import UserApiKey
from octomate.schemas.user import User, UserProfile
from octomate.types.auth import ApiKeyScope


async def a_user(
    username: str = "lu",
    *,
    name: str | None = None,
    nickname: str | None = None,
    profiles: dict[str, str] | None = None,
) -> User:
    user = User(
        username=username,
        name=name or username,
        nickname=nickname,
    )
    async with async_session() as session:
        session.add(user)
        await session.flush()
        for channel, account in (profiles or {}).items():
            session.add(
                UserProfile(
                    channel_tentacle_id=channel,
                    channel_user_id=account,
                    user_id=user.id,
                )
            )
        await session.commit()
    return user


def auth_config() -> AuthConfig:
    return AuthConfig(
        access_token_salt=SecretStr("access-token-test-salt"),
        refresh_token_salt=SecretStr("refresh-token-test-salt"),
        api_key_salt=SecretStr("api-key-test-salt"),
        cookie_secure=False,
    )


async def a_api_key(
    user: User, token: str, *, scopes: list[ApiKeyScope] | None = None
) -> UserApiKey:
    key = UserApiKey(
        user_id=user.id,
        name="Test client",
        key_prefix=token[:12],
        key_hash=AuthManager.hash_token(SecretStr(token), auth_config().api_key_salt),
        scopes=scopes if scopes is not None else ["hooks", "mcp"],
    )
    async with async_session() as session:
        session.add(key)
        await session.commit()
    return key
