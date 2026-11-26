from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.schemas.auth import RuntimeAccessToken, RuntimeGrant
from octomate.schemas.conversation import Conversation
from octomate.schemas.user import User
from tests.support.managers import a_thread
from tests.support.users import a_user


@pytest.fixture(autouse=True)
async def runtime_auth_db(in_memory_engine: AsyncEngine) -> None:
    return


@pytest.fixture
async def grant() -> RuntimeGrant:
    user = await a_user()
    grant = RuntimeGrant(
        user_id=user.id,
        agent_tentacle_id="codex",
        habitat_name="native",
        scopes=["mcp", "hooks"],
    )
    async with async_session() as session:
        session.add(grant)
        await session.commit()
    return grant


@pytest.fixture
async def token(grant: RuntimeGrant) -> RuntimeAccessToken:
    conversation = Conversation(
        thread_id=await a_thread(), agent_tentacle_id=grant.agent_tentacle_id
    )
    async with async_session() as session:
        session.add(conversation)
        await session.commit()
    token = RuntimeAccessToken(
        grant_id=grant.id,
        invocation_id=uuid7(),
        conversation_id=conversation.id,
        token_hash=SecretStr(hashlib.sha256(b"test-salt:test-token").hexdigest()),
        resource="https://core.example/mcp",
        scopes=["mcp"],
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    async with async_session() as session:
        session.add(token)
        await session.commit()
    return token


async def test_grant_revocation_and_token_bindings_survive_reload(
    grant: RuntimeGrant, token: RuntimeAccessToken
) -> None:
    revoked_at = datetime.now(UTC)
    async with async_session() as session:
        stored = await session.get(RuntimeGrant, grant.id)
        assert stored is not None
        stored.revoked_at = revoked_at
        await session.commit()

    async with async_session() as session:
        stored_grant = await session.get(RuntimeGrant, grant.id)
        stored_token = await session.get(RuntimeAccessToken, token.id)
    assert stored_grant is not None
    assert stored_grant.revoked_at == revoked_at
    assert stored_grant.user_id == grant.user_id
    assert stored_grant.agent_tentacle_id == grant.agent_tentacle_id
    assert stored_grant.habitat_name == grant.habitat_name
    assert stored_grant.scopes == ["mcp", "hooks"]
    assert stored_token is not None
    assert stored_token.grant_id == grant.id
    assert stored_token.invocation_id == token.invocation_id
    assert stored_token.conversation_id == token.conversation_id
    assert stored_token.resource == token.resource
    assert stored_token.scopes == ["mcp"]
    assert stored_token.expires_at == token.expires_at
    assert stored_token.created_at == token.created_at
    assert stored_token.token_hash == token.token_hash
    assert "token_hash" not in stored_token.model_dump()
    assert token.token_hash.get_secret_value() not in stored_token.model_dump_json()
    assert token.token_hash.get_secret_value() not in repr(stored_token)


@pytest.mark.parametrize("revoked", [False, True])
async def test_grant_identity_cannot_be_duplicated(
    grant: RuntimeGrant, revoked: bool
) -> None:
    async with async_session() as session:
        stored = await session.get(RuntimeGrant, grant.id)
        assert stored is not None
        if revoked:
            stored.revoked_at = datetime.now(UTC)
            await session.commit()
        session.add(
            RuntimeGrant(
                user_id=grant.user_id,
                agent_tentacle_id=grant.agent_tentacle_id,
                habitat_name=grant.habitat_name,
                scopes=["mcp"],
            )
        )
        with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
            await session.commit()


async def test_users_agents_and_habitats_have_independent_grants(
    grant: RuntimeGrant,
) -> None:
    other_user = await a_user("other")
    identities = [
        (other_user.id, grant.agent_tentacle_id, grant.habitat_name),
        (grant.user_id, "claude", grant.habitat_name),
        (grant.user_id, grant.agent_tentacle_id, "managed"),
    ]
    async with async_session() as session:
        for user_id, agent_id, habitat in identities:
            session.add(
                RuntimeGrant(
                    user_id=user_id,
                    agent_tentacle_id=agent_id,
                    habitat_name=habitat,
                    scopes=["mcp"],
                )
            )
        await session.commit()
    async with async_session() as session:
        assert len(await session.list(RuntimeGrant, limit=None)) == 4


@pytest.mark.parametrize("scopes", [[], ["acp"]])
def test_runtime_grants_reject_empty_or_inbound_acp_authority(
    scopes: list[str],
) -> None:
    with pytest.raises(ValidationError, match="scopes"):
        RuntimeGrant.model_validate(
            {
                "user_id": uuid7(),
                "agent_tentacle_id": "codex",
                "habitat_name": "native",
                "scopes": scopes,
            }
        )


async def test_same_bearer_hash_cannot_identify_two_invocations(
    token: RuntimeAccessToken,
) -> None:
    duplicate = RuntimeAccessToken(
        grant_id=token.grant_id,
        invocation_id=uuid7(),
        conversation_id=token.conversation_id,
        token_hash=token.token_hash,
        resource=token.resource,
        scopes=token.scopes,
        expires_at=token.expires_at,
    )
    async with async_session() as session:
        session.add(duplicate)
        with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
            await session.commit()


@pytest.mark.parametrize("parent", ["user", "grant", "conversation"])
async def test_parent_deletion_removes_tokens(
    grant: RuntimeGrant,
    token: RuntimeAccessToken,
    parent: Literal["user", "grant", "conversation"],
) -> None:
    async with async_session() as session:
        if parent == "user":
            stored = await session.get(User, grant.user_id)
        elif parent == "grant":
            stored = await session.get(RuntimeGrant, grant.id)
        else:
            stored = await session.get(Conversation, token.conversation_id)
        assert stored is not None
        await session.delete(stored)
        await session.commit()
    async with async_session() as session:
        assert await session.get(RuntimeAccessToken, token.id) is None
        stored_grant = await session.get(RuntimeGrant, grant.id)
        assert (stored_grant is not None) == (parent == "conversation")


async def test_removing_a_token_preserves_its_grant(
    grant: RuntimeGrant, token: RuntimeAccessToken
) -> None:
    async with async_session() as session:
        stored = await session.get(RuntimeAccessToken, token.id)
        assert stored is not None
        await session.delete(stored)
        await session.commit()
    async with async_session() as session:
        assert await session.get(RuntimeAccessToken, token.id) is None
        assert await session.get(RuntimeGrant, grant.id) is not None
