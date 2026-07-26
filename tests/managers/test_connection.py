from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.config.oauth import (
    McpOAuthConnectionConfig,
    OAuthConfig,
    OAuthSecuritySettings,
    ProviderOAuthConnectionConfig,
)
from octomate.config.users import UserConfig
from octomate.database import async_session
from octomate.managers.connection import ConnectionManager, OAuthCipher
from octomate.managers.user import UserManager
from octomate.schemas.oauth import (
    McpOAuthCompletion,
    McpOAuthConnection,
    OAuthCallbackContext,
    OAuthConnection,
    OAuthTransaction,
    ProviderOAuthCompletion,
)
from octomate.schemas.user import UserProfile


@pytest.fixture(autouse=True)
async def database(in_memory_engine: AsyncEngine) -> AsyncIterator[None]:
    yield


def encoded_key(byte: bytes) -> SecretStr:
    return SecretStr(base64.urlsafe_b64encode(byte * 32).decode())


def security(
    primary_key_id: str = "current",
    *,
    include_old: bool = False,
) -> OAuthSecuritySettings:
    keys = {"current": encoded_key(b"c")}
    if include_old:
        keys["old"] = encoded_key(b"o")
    return OAuthSecuritySettings(
        primary_key_id=primary_key_id,
        encryption_keys=keys,
    )


def oauth_config() -> OAuthConfig:
    return OAuthConfig(
        connections={
            "github": ProviderOAuthConnectionConfig(provider="github"),
            "linear-mcp": McpOAuthConnectionConfig.model_validate(
                {"resource_url": "https://mcp.linear.app/mcp"}
            ),
        }
    )


def manager() -> ConnectionManager:
    return ConnectionManager(oauth_config(), security())


async def linked_profile(
    username: str = "luhui",
    channel_user_id: str = "U1",
) -> UserProfile:
    return (await linked_profiles({username: channel_user_id}))[username]


async def linked_profiles(spec: dict[str, str]) -> dict[str, UserProfile]:
    users = UserManager(
        {
            username: UserConfig.model_validate(
                {
                    "profiles": {
                        "slack": {"channel_user_id": channel_user_id},
                    }
                }
            )
            for username, channel_user_id in spec.items()
        }
    )
    await users.reconcile()
    async with async_session() as session:
        profiles = list(
            await session.list(
                UserProfile,
                expressions=[UserProfile["user_id"].is_not(None)],
                limit=None,
            )
        )
    profiles_by_platform_id = {profile.channel_user_id: profile for profile in profiles}
    return {
        username: profiles_by_platform_id[channel_user_id]
        for username, channel_user_id in spec.items()
    }


async def complete_provider(
    connections: ConnectionManager,
    profile: UserProfile,
    *,
    replace: bool = False,
    access_token: str = "access-secret",
) -> tuple[OAuthCallbackContext, int]:
    authorization = await connections.begin(profile, "github", replace=replace)
    start = await connections.redeem_ticket(authorization.ticket)
    callback = await connections.claim_callback(start.state)
    summary = await connections.complete(
        callback,
        ProviderOAuthCompletion(
            tokens={
                "access_token": access_token,
                "refresh_token": "refresh-secret",
                "token_type": "bearer",
            },
            subject="github-user-1",
            account_label="lu@example.com",
            scopes=["repo"],
        ),
    )
    return callback, summary.version


def test_oauth_security_requires_a_declared_primary_key() -> None:
    with pytest.raises(ValidationError, match="primary_key_id"):
        OAuthSecuritySettings(
            primary_key_id="missing",
            encryption_keys={"current": encoded_key(b"c")},
        )


def test_oauth_security_reads_environment_but_not_yaml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "octomate.yaml").write_text(
        "octomate:\n  oauth:\n    primary_key_id: yaml\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OCTOMATE_OAUTH_PRIMARY_KEY_ID", raising=False)
    monkeypatch.delenv("OCTOMATE_OAUTH_ENCRYPTION_KEYS", raising=False)
    with pytest.raises(ValidationError):
        OAuthSecuritySettings()

    monkeypatch.setenv("OCTOMATE_OAUTH_PRIMARY_KEY_ID", "environment")
    monkeypatch.setenv(
        "OCTOMATE_OAUTH_ENCRYPTION_KEYS",
        json.dumps({"environment": encoded_key(b"e").get_secret_value()}),
    )

    settings = OAuthSecuritySettings()

    assert settings.primary_key_id == "environment"


def test_oauth_config_discriminates_provider_and_mcp_connections() -> None:
    config = OAuthConfig.model_validate(
        {
            "connections": {
                "github": {"kind": "provider", "provider": "github"},
                "linear": {
                    "kind": "mcp",
                    "resource_url": "https://mcp.linear.app/mcp",
                },
            }
        }
    )

    assert isinstance(config.connections["github"], ProviderOAuthConnectionConfig)
    assert isinstance(config.connections["linear"], McpOAuthConnectionConfig)


def test_cipher_authenticates_context_and_rotates_keys() -> None:
    old = OAuthCipher(
        OAuthSecuritySettings(
            primary_key_id="old",
            encryption_keys={"old": encoded_key(b"o")},
        )
    )
    encrypted = old.encrypt(
        {"access_token": "plaintext-secret"},
        context="connection:1:tokens",
    )
    rotated = OAuthCipher(security(include_old=True))

    assert b"plaintext-secret" not in encrypted
    assert rotated.decrypt(encrypted, context="connection:1:tokens") == {
        "access_token": "plaintext-secret"
    }
    with pytest.raises(ValueError, match="failed authentication"):
        rotated.decrypt(encrypted, context="connection:2:tokens")
    assert b'"key_id":"current"' in rotated.encrypt(
        {"access_token": "new"},
        context="connection:1:tokens",
    )


async def test_visitor_cannot_begin_a_connection() -> None:
    visitor = await UserManager().ensure_profile(
        "slack",
        UserProfile(channel_user_id="visitor", name="Visitor"),
    )

    with pytest.raises(ValueError, match="currently linked profile"):
        await manager().begin(visitor, "github")

    async with async_session() as session:
        assert list(await session.list(OAuthTransaction, limit=None)) == []


async def test_provider_flow_is_single_use_and_tokens_stay_encrypted() -> None:
    connections = manager()
    profile = await linked_profile()
    authorization = await connections.begin(
        profile,
        "github",
        data={"return_to": "settings"},
    )

    assert (
        authorization.ticket.get_secret_value() not in authorization.model_dump_json()
    )
    start = await connections.redeem_ticket(authorization.ticket)
    assert start.data == {"return_to": "settings"}
    assert start.code_challenge
    with pytest.raises(ValueError, match="invalid or expired OAuth ticket"):
        await connections.redeem_ticket(authorization.ticket)

    callback = await connections.claim_callback(start.state)
    assert callback.code_verifier.get_secret_value()
    assert callback.data == {"return_to": "settings"}
    with pytest.raises(ValueError, match="invalid or expired OAuth state"):
        await connections.claim_callback(start.state)

    summary = await connections.complete(
        callback,
        ProviderOAuthCompletion(
            tokens={
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
            },
            subject="github-user-1",
            account_label="Lu",
            scopes=["repo"],
        ),
    )

    assert summary.kind == "provider"
    assert summary.key == "github"
    assert summary.account_label == "Lu"
    assert await connections.get_token(profile, "github") == {
        "access_token": "access-secret",
        "refresh_token": "refresh-secret",
    }
    assert "access-secret" not in summary.model_dump_json()
    with pytest.raises(ValueError, match="invalid or consumed"):
        await connections.complete(
            callback,
            ProviderOAuthCompletion(tokens={"access_token": "replay"}),
        )

    async with async_session() as session:
        [stored] = list(await session.list(OAuthConnection, limit=None))
        transaction = await session.get(OAuthTransaction, authorization.transaction_id)
    assert b"access-secret" not in stored.encrypted_tokens
    assert "encrypted_tokens" not in stored.model_dump()
    assert transaction is not None and transaction.consumed_at is not None
    assert "encrypted_data" not in transaction.model_dump()


async def test_ticket_and_callback_claims_are_single_use_under_concurrency() -> None:
    connections = manager()
    profile = await linked_profile()
    authorization = await connections.begin(profile, "github")

    starts = await asyncio.gather(
        connections.redeem_ticket(authorization.ticket),
        connections.redeem_ticket(authorization.ticket),
        return_exceptions=True,
    )
    start_contexts = [
        result for result in starts if not isinstance(result, BaseException)
    ]
    assert len(start_contexts) == 1
    assert len([result for result in starts if isinstance(result, ValueError)]) == 1

    callbacks = await asyncio.gather(
        connections.claim_callback(start_contexts[0].state),
        connections.claim_callback(start_contexts[0].state),
        return_exceptions=True,
    )
    assert (
        len([result for result in callbacks if not isinstance(result, BaseException)])
        == 1
    )
    assert len([result for result in callbacks if isinstance(result, ValueError)]) == 1


async def test_connection_access_cannot_cross_users() -> None:
    connections = manager()
    profiles = await linked_profiles({"first": "U1", "second": "U2"})
    first = profiles["first"]
    second = profiles["second"]
    await complete_provider(connections, first)

    with pytest.raises(ValueError, match="not active"):
        await connections.get_token(second, "github")
    assert await connections.list(second) == []


async def test_unlinked_user_connection_becomes_dormant_without_deletion() -> None:
    connections = manager()
    profile = await linked_profile()
    await complete_provider(connections, profile)

    await UserManager().reconcile()

    with pytest.raises(ValueError, match="currently linked profile"):
        await connections.get_token(profile, "github")
    async with async_session() as session:
        assert len(list(await session.list(OAuthConnection, limit=None))) == 1


async def test_replacement_must_be_authorized_by_the_linked_profile() -> None:
    connections = manager()
    profile = await linked_profile()
    _, original_version = await complete_provider(connections, profile)

    with pytest.raises(ValueError, match="replacement is required"):
        await connections.begin(profile, "github")

    _, replacement_version = await complete_provider(
        connections,
        profile,
        replace=True,
        access_token="replacement-secret",
    )

    assert replacement_version > original_version
    assert await connections.get_token(profile, "github") == {
        "access_token": "replacement-secret",
        "refresh_token": "refresh-secret",
        "token_type": "bearer",
    }
    assert len(await connections.list(profile)) == 1


async def test_replacement_rejects_a_changed_connection_kind() -> None:
    connections = manager()
    profile = await linked_profile()
    await complete_provider(connections, profile)
    changed = ConnectionManager(
        OAuthConfig(
            connections={
                "github": McpOAuthConnectionConfig.model_validate(
                    {"resource_url": "https://example.com/mcp"}
                )
            }
        ),
        security(),
    )
    authorization = await changed.begin(profile, "github", replace=True)
    start = await changed.redeem_ticket(authorization.ticket)
    callback = await changed.claim_callback(start.state)

    with pytest.raises(ValueError, match="connection kind changed"):
        await changed.complete(
            callback,
            McpOAuthCompletion(
                tokens={"access_token": "mcp-secret"},
                authorization_server="https://example.com/oauth",
            ),
        )


async def test_refresh_uses_an_optimistic_version() -> None:
    connections = manager()
    profile = await linked_profile()
    _, version = await complete_provider(connections, profile)

    results = await asyncio.gather(
        connections.replace_tokens(
            profile,
            "github",
            expected_version=version,
            tokens={"access_token": "first", "refresh_token": "first-refresh"},
            expires_at=None,
        ),
        connections.replace_tokens(
            profile,
            "github",
            expected_version=version,
            tokens={"access_token": "second", "refresh_token": "second-refresh"},
            expires_at=None,
        ),
        return_exceptions=True,
    )

    successes = [result for result in results if not isinstance(result, BaseException)]
    failures = [result for result in results if isinstance(result, ValueError)]
    assert len(successes) == 1
    assert successes[0].version > version
    assert len(failures) == 1
    assert "refreshed concurrently" in str(failures[0])
    assert (await connections.get_token(profile, "github"))["access_token"] in {
        "first",
        "second",
    }


async def test_mcp_completion_encrypts_tokens_and_client_information() -> None:
    connections = manager()
    profile = await linked_profile()
    authorization = await connections.begin(profile, "linear-mcp")
    start = await connections.redeem_ticket(authorization.ticket)
    callback = await connections.claim_callback(start.state)

    summary = await connections.complete(
        callback,
        McpOAuthCompletion(
            tokens={"access_token": "mcp-secret"},
            authorization_server="https://linear.app/oauth",
            client_information={
                "client_id": "dynamic-client",
                "client_secret": "dynamic-secret",
            },
            account_label="Linear workspace",
        ),
    )

    assert summary.kind == "mcp"
    async with async_session() as session:
        stored = await session.get(McpOAuthConnection, summary.id)
    assert stored is not None
    assert stored.resource_url == "https://mcp.linear.app/mcp"
    assert stored.authorization_server == "https://linear.app/oauth"
    assert stored.encrypted_client_information is not None
    assert b"dynamic-secret" not in stored.encrypted_client_information


async def test_expired_transactions_are_rejected_and_cleaned_up() -> None:
    connections = manager()
    profile = await linked_profile()
    authorization = await connections.begin(
        profile,
        "github",
        ttl=timedelta(seconds=-1),
    )

    with pytest.raises(ValueError, match="invalid or expired OAuth ticket"):
        await connections.redeem_ticket(authorization.ticket)
    assert await connections.cleanup_transactions() == 1
    async with async_session() as session:
        assert await session.get(OAuthTransaction, authorization.transaction_id) is None


async def test_unlinking_the_profile_invalidates_an_inflight_callback() -> None:
    connections = manager()
    profile = await linked_profile()
    authorization = await connections.begin(profile, "github")
    start = await connections.redeem_ticket(authorization.ticket)

    await UserManager().reconcile()

    with pytest.raises(ValueError, match="no longer linked"):
        await connections.claim_callback(start.state)


async def test_disconnect_removes_only_the_current_users_connection() -> None:
    connections = manager()
    profiles = await linked_profiles({"first": "U1", "second": "U2"})
    first = profiles["first"]
    second = profiles["second"]
    await complete_provider(connections, first)
    await complete_provider(connections, second)

    removed = await connections.disconnect(first, "github")

    assert removed.key == "github"
    assert await connections.list(first) == []
    assert len(await connections.list(second)) == 1
