"""Inbound OAuth grant lifecycle; protocol request validation is a separate layer."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal
from unittest.mock import patch

import pytest
from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.config.auth import AuthConfig
from octomate.database import async_session
from octomate.managers.auth import AuthManager, InvalidCredentials, InvalidOAuthScopes
from octomate.schemas.authorization import (
    OAuthAuthorization,
    OAuthClient,
    OAuthGrant,
    OAuthToken,
)
from octomate.schemas.user import User
from octomate.types.auth import ApiKeyScope

RESOURCE = "https://octomate.test/octomate/mcp"
TICKET = SecretStr("browser-consent-ticket")


@pytest.fixture(autouse=True)
async def authorization_db(in_memory_engine: AsyncEngine) -> None:
    return


@pytest.fixture
def auth() -> AuthManager:
    return AuthManager(
        AuthConfig(
            access_token_salt=SecretStr("access-token-test-salt"),
            refresh_token_salt=SecretStr("refresh-token-test-salt"),
            api_key_salt=SecretStr("api-key-test-salt"),
        )
    )


@pytest.fixture
async def user() -> User:
    user = User(username="alice")
    async with async_session() as session:
        session.add(user)
        await session.commit()
    return user


@pytest.fixture
async def client() -> OAuthClient:
    client = OAuthClient(
        name="Native harness",
        redirect_uris=["http://127.0.0.1/callback"],
        scopes=["mcp", "hooks"],
    )
    async with async_session() as session:
        session.add(client)
        await session.commit()
    return client


@pytest.fixture
async def pending(auth: AuthManager, client: OAuthClient) -> OAuthAuthorization:
    # The protocol layer will validate the redirect, resource, and S256 request.
    pending = OAuthAuthorization(
        client_id=client.id,
        ticket_hash=auth.hash_token(TICKET, auth.config.access_token_salt),
        redirect_uri=client.redirect_uris[0],
        redirect_uri_provided_explicitly=True,
        resource=RESOURCE,
        scopes=["mcp", "hooks"],
        code_challenge="A" * 43,
        state=SecretStr("client-callback-state"),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    async with async_session() as session:
        session.add(pending)
        await session.commit()
    return pending


@pytest.fixture
async def approved(
    auth: AuthManager, user: User, pending: OAuthAuthorization
) -> OAuthAuthorization:
    return await auth.approve_oauth_authorization(user.id, TICKET)


@pytest.fixture
async def issued(
    auth: AuthManager, approved: OAuthAuthorization, client: OAuthClient
) -> OAuthToken:
    assert approved.code is not None
    return await auth.exchange_oauth_code(client.id, approved.code, resource=RESOURCE)


async def test_consent_is_bound_once_and_secrets_are_not_persisted(
    auth: AuthManager,
    user: User,
    client: OAuthClient,
    approved: OAuthAuthorization,
    issued: OAuthToken,
) -> None:
    assert approved.code is not None
    assert issued.access_token is not None
    assert issued.refresh_token is not None
    assert issued.access_token != issued.refresh_token
    async with async_session() as session:
        saved = await session.get(OAuthAuthorization, approved.id)
        token = await session.get(OAuthToken, issued.id)
        grant = await session.get(OAuthGrant, issued.grant_id)
    assert saved is not None
    assert saved.consumed_at is not None
    assert saved.code is None
    assert saved.code_hash == auth.hash_token(
        approved.code, auth.config.access_token_salt
    )
    assert token is not None
    assert token.access_token is None
    assert token.refresh_token is None
    assert token.access_token_hash == auth.hash_token(
        issued.access_token, auth.config.access_token_salt
    )
    assert token.refresh_token_hash == auth.hash_token(
        issued.refresh_token, auth.config.refresh_token_salt
    )
    assert grant is not None
    assert grant.user_id == user.id
    assert grant.client_id == client.id
    assert grant.resource == RESOURCE
    assert issued.access_expires_at < grant.expires_at
    with pytest.raises(InvalidCredentials):
        await auth.approve_oauth_authorization(user.id, TICKET)
    for item in (approved, issued, saved, token):
        for secret in (
            TICKET,
            approved.code,
            issued.access_token,
            issued.refresh_token,
        ):
            assert secret.get_secret_value() not in str(item)
            assert secret.get_secret_value() not in repr(item)
            assert secret.get_secret_value() not in item.model_dump_json()


@pytest.mark.parametrize("invalid", ["ticket", "user", "expired", "scopes"])
async def test_invalid_consent_issues_no_grant(
    auth: AuthManager,
    user: User,
    pending: OAuthAuthorization,
    invalid: str,
) -> None:
    async with async_session() as session:
        saved = await session.get(OAuthAuthorization, pending.id)
        assert saved is not None
        if invalid == "expired":
            saved.expires_at = datetime.now(UTC)
        if invalid == "scopes":
            saved.scopes = ["acp"]
        await session.commit()
    with pytest.raises(
        InvalidOAuthScopes if invalid == "scopes" else InvalidCredentials
    ):
        await auth.approve_oauth_authorization(
            uuid7() if invalid == "user" else user.id,
            SecretStr("unknown") if invalid == "ticket" else TICKET,
        )
    async with async_session() as session:
        assert list(await session.list(OAuthGrant, limit=None)) == []


async def test_restart_and_scope_narrowing_renew_grant_idle_deadline(
    auth: AuthManager,
    client: OAuthClient,
    user: User,
    issued: OAuthToken,
) -> None:
    assert issued.refresh_token is not None
    async with async_session() as session:
        grant = await session.get(OAuthGrant, issued.grant_id)
        assert grant is not None
        deadline = datetime.now(UTC) + timedelta(seconds=30)
        grant.expires_at = deadline
        await session.commit()
    restarted = AuthManager(auth.config)
    before_refresh = datetime.now(UTC)
    second = await restarted.refresh_oauth_token(
        client.id, issued.refresh_token, resource=RESOURCE, scopes=["mcp"]
    )
    assert second.grant_id == issued.grant_id
    assert second.access_token != issued.access_token
    assert second.refresh_token != issued.refresh_token
    assert second.access_expires_at > deadline
    assert (
        second.access_expires_at
        <= datetime.now(UTC) + auth.config.access_token_lifetime
    )
    assert second.access_token is not None
    assert second.refresh_token is not None
    identity = await restarted.authenticate_oauth_token(
        second.access_token, resource=RESOURCE, scope="mcp"
    )
    assert identity is not None
    assert identity.user_id == user.id
    assert identity.client_id == client.id
    assert identity.grant_id == issued.grant_id
    assert identity.scopes == ["mcp"]
    assert (
        await restarted.authenticate_oauth_token(
            second.access_token, resource=RESOURCE, scope="hooks"
        )
        is None
    )
    third = await restarted.refresh_oauth_token(
        client.id, second.refresh_token, resource=RESOURCE
    )
    assert third.scopes == ["mcp"]
    async with async_session() as session:
        grant = await session.get(OAuthGrant, issued.grant_id)
        assert grant is not None
        assert before_refresh + timedelta(days=30) <= grant.expires_at
        assert grant.expires_at <= datetime.now(UTC) + timedelta(days=30)
        assert len(await session.list(OAuthGrant, limit=None)) == 1


async def test_only_successful_refresh_extends_idle_expiry_without_an_absolute_limit(
    auth: AuthManager, client: OAuthClient, issued: OAuthToken
) -> None:
    start = datetime.now(UTC)
    current = issued
    for days in (29, 58, 87):
        assert current.refresh_token is not None
        with patch("octomate.managers.auth.datetime") as clock:
            clock.now.return_value = start + timedelta(days=days)
            current = await auth.refresh_oauth_token(
                client.id, current.refresh_token, resource=RESOURCE
            )
    deadline = start + timedelta(days=117)
    assert current.access_token is not None
    assert current.refresh_token is not None
    with patch("octomate.managers.auth.datetime") as clock:
        clock.now.return_value = start + timedelta(days=87, minutes=5)
        assert (
            await auth.authenticate_oauth_token(
                current.access_token, resource=RESOURCE, scope="mcp"
            )
            is not None
        )
        with pytest.raises(InvalidOAuthScopes):
            await auth.refresh_oauth_token(
                client.id, current.refresh_token, resource=RESOURCE, scopes=["acp"]
            )
    async with async_session() as session:
        grant = await session.get(OAuthGrant, issued.grant_id)
        assert grant is not None
        assert grant.expires_at == deadline
    with patch("octomate.managers.auth.datetime") as clock:
        clock.now.return_value = deadline
        with pytest.raises(InvalidCredentials):
            await auth.refresh_oauth_token(
                client.id, current.refresh_token, resource=RESOURCE
            )
    async with async_session() as session:
        grant = await session.get(OAuthGrant, issued.grant_id)
        assert grant is not None
        assert grant.expires_at == deadline


@pytest.mark.parametrize(
    "invalid",
    ["secret", "resource", "scope", "token_expired", "grant_expired", "revoked"],
)
async def test_access_rejects_invalid_or_inactive_credentials(
    auth: AuthManager,
    issued: OAuthToken,
    invalid: str,
) -> None:
    assert issued.access_token is not None
    async with async_session() as session:
        token = await session.get(OAuthToken, issued.id)
        grant = await session.get(OAuthGrant, issued.grant_id)
        assert token is not None
        assert grant is not None
        if invalid == "token_expired":
            token.access_expires_at = datetime.now(UTC)
        if invalid == "grant_expired":
            grant.expires_at = datetime.now(UTC)
        if invalid == "revoked":
            grant.revoked_at = datetime.now(UTC)
        await session.commit()
    assert (
        await auth.authenticate_oauth_token(
            SecretStr("unknown") if invalid == "secret" else issued.access_token,
            resource="https://other.test/mcp" if invalid == "resource" else RESOURCE,
            scope="acp" if invalid == "scope" else "mcp",
        )
        is None
    )


@pytest.mark.parametrize("invalid", ["client", "resource", "secret"])
async def test_wrong_code_binding_does_not_consume_or_revoke(
    auth: AuthManager,
    client: OAuthClient,
    approved: OAuthAuthorization,
    invalid: str,
) -> None:
    assert approved.code is not None
    with pytest.raises(InvalidCredentials):
        await auth.exchange_oauth_code(
            uuid7() if invalid == "client" else client.id,
            SecretStr("unknown") if invalid == "secret" else approved.code,
            resource="https://other.test/mcp" if invalid == "resource" else RESOURCE,
        )
    await auth.exchange_oauth_code(client.id, approved.code, resource=RESOURCE)


async def test_expired_code_cannot_be_exchanged(
    auth: AuthManager, client: OAuthClient, approved: OAuthAuthorization
) -> None:
    assert approved.code is not None
    async with async_session() as session:
        saved = await session.get(OAuthAuthorization, approved.id)
        assert saved is not None
        saved.expires_at = datetime.now(UTC)
        await session.commit()
    with pytest.raises(InvalidCredentials):
        await auth.exchange_oauth_code(client.id, approved.code, resource=RESOURCE)
    async with async_session() as session:
        assert list(await session.list(OAuthToken, limit=None)) == []


@pytest.mark.parametrize("invalid", ["client", "resource", "secret"])
async def test_wrong_refresh_binding_does_not_consume_or_revoke(
    auth: AuthManager,
    client: OAuthClient,
    issued: OAuthToken,
    invalid: str,
) -> None:
    assert issued.refresh_token is not None
    with pytest.raises(InvalidCredentials):
        await auth.refresh_oauth_token(
            uuid7() if invalid == "client" else client.id,
            SecretStr("unknown") if invalid == "secret" else issued.refresh_token,
            resource="https://other.test/mcp" if invalid == "resource" else RESOURCE,
        )
    await auth.refresh_oauth_token(client.id, issued.refresh_token, resource=RESOURCE)


@pytest.mark.parametrize("scopes", [[], ["acp"], ["mcp", "hooks"]])
async def test_refresh_cannot_widen_a_narrowed_generation(
    auth: AuthManager,
    client: OAuthClient,
    issued: OAuthToken,
    scopes: list[ApiKeyScope],
) -> None:
    assert issued.refresh_token is not None
    narrowed = await auth.refresh_oauth_token(
        client.id, issued.refresh_token, resource=RESOURCE, scopes=["mcp"]
    )
    assert narrowed.refresh_token is not None
    with pytest.raises(InvalidOAuthScopes):
        await auth.refresh_oauth_token(
            client.id, narrowed.refresh_token, resource=RESOURCE, scopes=scopes
        )
    await auth.refresh_oauth_token(client.id, narrowed.refresh_token, resource=RESOURCE)


@pytest.mark.parametrize("replay", ["code", "refresh"])
async def test_replay_revokes_every_generation(
    auth: AuthManager,
    client: OAuthClient,
    approved: OAuthAuthorization,
    issued: OAuthToken,
    replay: str,
) -> None:
    assert issued.refresh_token is not None
    assert approved.code is not None
    second = await auth.refresh_oauth_token(
        client.id, issued.refresh_token, resource=RESOURCE
    )
    replayed = (
        auth.exchange_oauth_code(client.id, approved.code, resource=RESOURCE)
        if replay == "code"
        else auth.refresh_oauth_token(
            client.id, issued.refresh_token, resource=RESOURCE
        )
    )
    with pytest.raises(InvalidCredentials):
        await replayed
    restarted = AuthManager(auth.config)
    for token in (issued, second):
        assert token.access_token is not None
        assert token.refresh_token is not None
        assert (
            await restarted.authenticate_oauth_token(
                token.access_token, resource=RESOURCE, scope="mcp"
            )
            is None
        )
        with pytest.raises(InvalidCredentials):
            await restarted.refresh_oauth_token(
                client.id, token.refresh_token, resource=RESOURCE
            )


@pytest.mark.parametrize("separate_managers", [False, True])
@pytest.mark.parametrize("operation", ["approve", "code", "refresh"])
async def test_concurrent_consumption_has_one_winner(
    auth: AuthManager,
    user: User,
    client: OAuthClient,
    pending: OAuthAuthorization,
    operation: Literal["approve", "code", "refresh"],
    separate_managers: bool,
) -> None:
    other = AuthManager(auth.config) if separate_managers else auth
    if operation == "approve":
        outcomes = await asyncio.gather(
            auth.approve_oauth_authorization(user.id, TICKET),
            other.approve_oauth_authorization(user.id, TICKET),
            return_exceptions=True,
        )
        assert sum(isinstance(item, OAuthAuthorization) for item in outcomes) == 1
        assert sum(isinstance(item, InvalidCredentials) for item in outcomes) == 1
        return
    approved = await auth.approve_oauth_authorization(user.id, TICKET)
    assert approved.code is not None
    if operation == "code":
        outcomes = await asyncio.gather(
            auth.exchange_oauth_code(client.id, approved.code, resource=RESOURCE),
            other.exchange_oauth_code(client.id, approved.code, resource=RESOURCE),
            return_exceptions=True,
        )
    else:
        issued = await auth.exchange_oauth_code(
            client.id, approved.code, resource=RESOURCE
        )
        assert issued.refresh_token is not None
        outcomes = await asyncio.gather(
            auth.refresh_oauth_token(
                client.id, issued.refresh_token, resource=RESOURCE
            ),
            other.refresh_oauth_token(
                client.id, issued.refresh_token, resource=RESOURCE
            ),
            return_exceptions=True,
        )
    winners = [item for item in outcomes if isinstance(item, OAuthToken)]
    assert len(winners) == 1
    assert sum(isinstance(item, InvalidCredentials) for item in outcomes) == 1
    assert winners[0].access_token is not None
    assert (
        await auth.authenticate_oauth_token(
            winners[0].access_token, resource=RESOURCE, scope="mcp"
        )
        is None
    )


async def test_revoke_checks_owner_and_does_not_reactivate(
    auth: AuthManager,
    user: User,
    client: OAuthClient,
    issued: OAuthToken,
) -> None:
    assert issued.access_token is not None
    assert issued.refresh_token is not None
    with pytest.raises(InvalidCredentials):
        await auth.revoke_oauth_grant(uuid7(), issued.grant_id)
    assert (
        await auth.authenticate_oauth_token(
            issued.access_token, resource=RESOURCE, scope="mcp"
        )
        is not None
    )
    await auth.revoke_oauth_grant(user.id, issued.grant_id)
    await auth.revoke_oauth_grant(user.id, issued.grant_id)
    assert (
        await auth.authenticate_oauth_token(
            issued.access_token, resource=RESOURCE, scope="mcp"
        )
        is None
    )
    with pytest.raises(InvalidCredentials):
        await AuthManager(auth.config).refresh_oauth_token(
            client.id, issued.refresh_token, resource=RESOURCE
        )


async def test_failed_refresh_rolls_back_consumption(
    auth: AuthManager,
    client: OAuthClient,
    issued: OAuthToken,
) -> None:
    assert issued.access_token is not None
    assert issued.refresh_token is not None
    # Force the replacement's access hash to collide with the existing row.
    with patch(
        "octomate.managers.auth.secrets.token_urlsafe",
        return_value=issued.access_token.get_secret_value(),
    ):
        with pytest.raises(IntegrityError):
            await auth.refresh_oauth_token(
                client.id, issued.refresh_token, resource=RESOURCE
            )
    replacement = await auth.refresh_oauth_token(
        client.id, issued.refresh_token, resource=RESOURCE
    )
    assert replacement.access_token is not None
    assert (
        await auth.authenticate_oauth_token(
            replacement.access_token, resource=RESOURCE, scope="mcp"
        )
        is not None
    )


async def test_access_expiry_does_not_end_refresh_authority(
    auth: AuthManager, client: OAuthClient, issued: OAuthToken
) -> None:
    assert issued.access_token is not None
    assert issued.refresh_token is not None
    async with async_session() as session:
        token = await session.get(OAuthToken, issued.id)
        assert token is not None
        token.access_expires_at = datetime.now(UTC)
        await session.commit()
    replacement = await auth.refresh_oauth_token(
        client.id, issued.refresh_token, resource=RESOURCE
    )
    assert replacement.access_token is not None
    assert (
        await auth.authenticate_oauth_token(
            issued.access_token, resource=RESOURCE, scope="mcp"
        )
        is None
    )
    assert (
        await auth.authenticate_oauth_token(
            replacement.access_token, resource=RESOURCE, scope="mcp"
        )
        is not None
    )


@pytest.mark.parametrize("operation", ["code", "refresh"])
@pytest.mark.parametrize("inactive", ["expired", "revoked"])
async def test_inactive_grant_cannot_issue_tokens(
    auth: AuthManager,
    client: OAuthClient,
    approved: OAuthAuthorization,
    operation: str,
    inactive: str,
) -> None:
    assert approved.code is not None
    assert approved.grant_id is not None
    credential = approved.code
    if operation == "refresh":
        issued = await auth.exchange_oauth_code(
            client.id, credential, resource=RESOURCE
        )
        assert issued.refresh_token is not None
        credential = issued.refresh_token
    async with async_session() as session:
        grant = await session.get(OAuthGrant, approved.grant_id)
        assert grant is not None
        if inactive == "expired":
            grant.expires_at = datetime.now(UTC)
        else:
            grant.revoked_at = datetime.now(UTC)
        await session.commit()
    exchange = (
        auth.exchange_oauth_code(client.id, credential, resource=RESOURCE)
        if operation == "code"
        else auth.refresh_oauth_token(client.id, credential, resource=RESOURCE)
    )
    with pytest.raises(InvalidCredentials):
        await exchange
    async with async_session() as session:
        assert len(await session.list(OAuthToken, limit=None)) == (
            1 if operation == "refresh" else 0
        )


async def test_revocation_racing_refresh_leaves_no_usable_tokens(
    auth: AuthManager,
    user: User,
    client: OAuthClient,
    issued: OAuthToken,
) -> None:
    assert issued.refresh_token is not None
    other = AuthManager(auth.config)
    outcomes = await asyncio.gather(
        auth.refresh_oauth_token(client.id, issued.refresh_token, resource=RESOURCE),
        other.revoke_oauth_grant(user.id, issued.grant_id),
        return_exceptions=True,
    )
    assert outcomes[1] is None
    assert isinstance(outcomes[0], (OAuthToken, InvalidCredentials))
    tokens = [issued, *[item for item in outcomes if isinstance(item, OAuthToken)]]
    for token in tokens:
        assert token.access_token is not None
        assert token.refresh_token is not None
        assert (
            await other.authenticate_oauth_token(
                token.access_token, resource=RESOURCE, scope="mcp"
            )
            is None
        )
        with pytest.raises(InvalidCredentials):
            await other.refresh_oauth_token(
                client.id, token.refresh_token, resource=RESOURCE
            )


async def test_revocation_leaves_another_grant_usable(
    auth: AuthManager,
    user: User,
    client: OAuthClient,
    pending: OAuthAuthorization,
    issued: OAuthToken,
) -> None:
    ticket = SecretStr("separate-installation-ticket")
    another = OAuthAuthorization(
        client_id=client.id,
        ticket_hash=auth.hash_token(ticket, auth.config.access_token_salt),
        redirect_uri=pending.redirect_uri,
        redirect_uri_provided_explicitly=True,
        resource=RESOURCE,
        scopes=["mcp"],
        code_challenge="B" * 43,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    async with async_session() as session:
        session.add(another)
        await session.commit()
    approved = await auth.approve_oauth_authorization(user.id, ticket)
    assert approved.code is not None
    second = await auth.exchange_oauth_code(client.id, approved.code, resource=RESOURCE)
    await auth.revoke_oauth_grant(user.id, issued.grant_id)
    assert second.access_token is not None
    assert second.refresh_token is not None
    assert (
        await auth.authenticate_oauth_token(
            second.access_token, resource=RESOURCE, scope="mcp"
        )
        is not None
    )
    await auth.refresh_oauth_token(client.id, second.refresh_token, resource=RESOURCE)


async def test_wrong_binding_cannot_use_spent_token_to_revoke(
    auth: AuthManager,
    client: OAuthClient,
    issued: OAuthToken,
) -> None:
    assert issued.refresh_token is not None
    second = await auth.refresh_oauth_token(
        client.id, issued.refresh_token, resource=RESOURCE
    )
    with pytest.raises(InvalidCredentials):
        await auth.refresh_oauth_token(uuid7(), issued.refresh_token, resource=RESOURCE)
    with pytest.raises(InvalidCredentials):
        await auth.refresh_oauth_token(
            client.id, issued.refresh_token, resource="https://other.test/mcp"
        )
    assert second.access_token is not None
    assert (
        await auth.authenticate_oauth_token(
            second.access_token, resource=RESOURCE, scope="mcp"
        )
        is not None
    )
