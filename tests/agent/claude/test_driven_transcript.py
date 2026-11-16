"""A driven session's transcript, uploaded by the machine the turn runs on,
is kept as its conversation's own bytes."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import pytest
from octomate_protocol.stream import TranscriptKept
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import ClaudeCodeConfig, OctomateConfig
from octomate.config.habitat import SbxHabitatConfig
from octomate.schemas.conversation import Conversation
from octomate.schemas.habitat import InvocationScope
from octomate.schemas.user import User
from octomate.tentacles.claude import ClaudeTentacle
from tests.support.managers import a_thread
from tests.support.users import a_api_key, a_user, auth_config
from tests.test_habitat_contracts import IMAGE

SECRET = SecretStr("the-hook-secret")
AUTH = {"Authorization": f"Bearer {SECRET.get_secret_value()}"}
FIRST = b'{"type":"user","uuid":"u1"}\n'
SECOND = b'{"type":"assistant","uuid":"a1"}\n{"type":"last-prompt"}\n'


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


@asynccontextmanager
async def a_driven_turn() -> AsyncGenerator[
    tuple[httpx.AsyncClient, Octomate, Conversation, User]
]:
    """A served tentacle, with a turn of one conversation inside its owner's sandbox."""
    octomate = Octomate(
        config=OctomateConfig(auth=auth_config(), habitat=SbxHabitatConfig(image=IMAGE))
    )
    tentacle = octomate.connect(
        ClaudeTentacle(
            octomate,
            auth=octomate.auth,
            habitats=octomate.habitats,
            config=ClaudeCodeConfig(),
            commands=octomate.commands,
            projects=octomate.projects,
            threads=octomate.threads,
            files=octomate.files,
            conversations=octomate.conversations,
            deferred_actions=octomate.deferred_actions,
            workspaces=octomate.workspaces,
            users=octomate.users,
            bearers=octomate.bearers,
            mcp=octomate.mcp,
        )
    )
    user = await a_user(profiles={"claude": "lu"})
    await a_api_key(user, SECRET.get_secret_value())
    conversation = await octomate.conversations.ensure(
        await a_thread(), agent_tentacle_id=tentacle.id
    )
    scope = InvocationScope(user_id=user.id, conversation_id=conversation.id)
    octomate.habitats.residence(scope).conversations[conversation.id] += 1
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=octomate), base_url="http://core"
    ) as client:
        yield client, octomate, conversation, user


async def kept_bytes(
    octomate: Octomate, conversation: Conversation, user: User
) -> bytes:
    stored = await octomate.conversations.get(conversation.id)
    assert stored.transcript_file_id is not None
    return await octomate.files.read(stored.transcript_file_id, owner_id=user.id)


async def test_uploads_append_where_the_kept_bytes_end() -> None:
    async with a_driven_turn() as (client, octomate, conversation, user):
        url = f"/hooks/claude/transcripts/{conversation.id}"

        asked = await client.post(url, params={"offset": 0}, headers=AUTH)
        assert asked.status_code == 200
        assert TranscriptKept.model_validate(asked.json()).size == 0

        first = await client.post(
            url, params={"offset": 0}, content=FIRST, headers=AUTH
        )
        assert first.status_code == 200
        assert TranscriptKept.model_validate(first.json()).size == len(FIRST)

        second = await client.post(
            url, params={"offset": len(FIRST)}, content=SECOND, headers=AUTH
        )
        assert TranscriptKept.model_validate(second.json()).size == len(FIRST + SECOND)
        assert await kept_bytes(octomate, conversation, user) == FIRST + SECOND


async def test_bytes_that_do_not_continue_the_kept_ones_are_refused() -> None:
    async with a_driven_turn() as (client, octomate, conversation, user):
        url = f"/hooks/claude/transcripts/{conversation.id}"
        await client.post(url, params={"offset": 0}, content=FIRST, headers=AUTH)

        resent = await client.post(
            url, params={"offset": 0}, content=FIRST, headers=AUTH
        )
        assert resent.status_code == 409
        # The refusal says where to resume.
        assert TranscriptKept.model_validate(resent.json()).size == len(FIRST)

        partial = await client.post(
            url, params={"offset": len(FIRST)}, content=b'{"type":"assis', headers=AUTH
        )
        assert partial.status_code == 422
        assert await kept_bytes(octomate, conversation, user) == FIRST


async def test_only_the_owner_uploads_and_only_while_the_turn_runs() -> None:
    async with a_driven_turn() as (client, octomate, conversation, user):
        url = f"/hooks/claude/transcripts/{conversation.id}"
        stranger = await a_user("mo", profiles={"claude": "mo"})
        await a_api_key(stranger, "another-hook-secret")

        unsigned = await client.post(url, params={"offset": 0}, content=FIRST)
        assert unsigned.status_code == 401
        foreign = await client.post(
            url,
            params={"offset": 0},
            content=FIRST,
            headers={"Authorization": "Bearer another-hook-secret"},
        )
        assert foreign.status_code == 404

        octomate.habitats.residences[user.id].conversations.clear()
        late = await client.post(url, params={"offset": 0}, content=FIRST, headers=AUTH)
        assert late.status_code == 404
        stored = await octomate.conversations.get(conversation.id)
        assert stored.transcript_file_id is None


async def test_another_agents_conversation_is_not_uploaded_here() -> None:
    async with a_driven_turn() as (client, octomate, conversation, user):
        other = await octomate.conversations.ensure(
            conversation.thread_id, agent_tentacle_id="codex"
        )
        scope = InvocationScope(user_id=user.id, conversation_id=other.id)
        octomate.habitats.residence(scope).conversations[other.id] += 1

        refused = await client.post(
            f"/hooks/claude/transcripts/{other.id}",
            params={"offset": 0},
            content=FIRST,
            headers=AUTH,
        )
        assert refused.status_code == 404
