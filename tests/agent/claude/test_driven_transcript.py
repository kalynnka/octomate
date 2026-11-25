"""A driven session's transcript, uploaded by the machine the turn runs on,
is kept as its conversation's own bytes."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx2
import pytest
import uvicorn
from click.testing import Result
from fastapi import Request, Response, UploadFile
from octomate_cli.config import CLISettings, cli_settings
from octomate_cli.main import app
from octomate_protocol.stream import TranscriptKept
from pydantic import UUID7, SecretStr
from pydantic_ai.models.test import TestModel
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.base import RequestResponseEndpoint
from typer.testing import CliRunner
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config import ClaudeCodeConfig, OctomateConfig
from octomate.config.agents import CodexConfig, DeepseekConfig
from octomate.config.habitat import SbxHabitatConfig
from octomate.managers.files import FileManager
from octomate.schemas.conversation import Conversation
from octomate.schemas.files import FileVariant
from octomate.schemas.habitat import InvocationScope
from octomate.schemas.user import User
from octomate.tentacles.claude import ClaudeTentacle
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.deepseek import DeepseekTentacle
from octomate.tentacles.inkling import InklingTentacle
from tests.habitat.test_contracts import IMAGE
from tests.support.managers import a_thread
from tests.support.users import a_api_key, a_user, auth_config

SECRET = SecretStr("the-hook-secret")
AUTH = {"Authorization": f"Bearer {SECRET.get_secret_value()}"}
FIRST = b'{"type":"user","uuid":"u1"}\n'
SECOND = b'{"type":"assistant","uuid":"a1"}\n{"type":"last-prompt"}\n'


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


@asynccontextmanager
async def a_driven_turn(
    *,
    image: str = IMAGE,
    agent_id: Literal["claude", "codex", "deepseek", "inkling"] = "claude",
) -> AsyncGenerator[tuple[httpx2.AsyncClient, Octomate, Conversation, User]]:
    """A served tentacle, with a turn of one conversation inside its owner's sandbox."""
    octomate = Octomate(
        config=OctomateConfig(auth=auth_config(), habitat=SbxHabitatConfig(image=image))
    )
    tentacle: ClaudeTentacle | CodexTentacle | DeepseekTentacle | InklingTentacle
    if agent_id == "claude":
        tentacle = ClaudeTentacle(
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
    elif agent_id == "codex":
        tentacle = CodexTentacle(
            octomate,
            auth=octomate.auth,
            habitats=octomate.habitats,
            config=CodexConfig(),
            commands=octomate.commands,
            projects=octomate.projects,
            threads=octomate.threads,
            files=octomate.files,
            conversations=octomate.conversations,
            deferred_actions=octomate.deferred_actions,
            workspaces=octomate.workspaces,
            users=octomate.users,
            bearers=octomate.bearers,
            gateway_manager=octomate.gateway,
        )
    elif agent_id == "deepseek":
        tentacle = DeepseekTentacle(
            octomate,
            auth=octomate.auth,
            habitats=octomate.habitats,
            config=DeepseekConfig(),
        )
    else:
        tentacle = InklingTentacle(
            "inkling",
            octomate,
            auth=octomate.auth,
            habitats=octomate.habitats,
            models={"test": TestModel()},
        )
    octomate.connect(tentacle)
    user = await a_user()
    await a_api_key(user, SECRET.get_secret_value())
    conversation = await octomate.conversations.ensure(
        await a_thread(), agent_tentacle_id=tentacle.id
    )
    scope = InvocationScope(user_id=user.id, conversation_id=conversation.id)
    octomate.habitats.residence(scope).conversations[conversation.id] += 1
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=octomate), base_url="http://core"
    ) as client:
        yield client, octomate, conversation, user


@asynccontextmanager
async def served(octomate: Octomate) -> AsyncGenerator[str]:
    """The app on a local port, for a client that speaks HTTP for real."""
    server = uvicorn.Server(
        uvicorn.Config(octomate, lifespan="off", ws="none", log_config=None)
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        # Listening already, so a request sent before the server accepts waits.
        sock.listen()
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            yield f"http://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            server.should_exit = True
            await serving


async def cli_upload(
    base: str,
    conversation: Conversation,
    path: Path,
    token: str,
    *,
    hook_event: Literal["Stop", "SessionEnd", "SubagentStop"] = "Stop",
    child: Path | None = None,
) -> Result:
    """The harness's upload command, run on the machine holding its transcript."""
    cli_settings.cache_clear()
    arguments = [
        conversation.agent_tentacle_id,
        "upload",
        "--conversation",
        str(conversation.id),
    ]
    payload = None
    if conversation.agent_tentacle_id == "claude":
        payload = json.dumps(
            {
                "hook_event_name": hook_event,
                "session_id": str(uuid7()),
                "transcript_path": str(path),
                **(
                    {
                        "agent_id": child.stem.removeprefix("agent-"),
                        "agent_transcript_path": str(child),
                    }
                    if child
                    else {}
                ),
            }
        )
    else:
        arguments.extend(["--path", str(path)])
    return await asyncio.to_thread(
        CliRunner().invoke,
        app,
        arguments,
        input=payload,
        env={CLISettings.env("url"): base, CLISettings.env("token"): token},
    )


async def kept_bytes(
    octomate: Octomate, conversation: Conversation, user: User
) -> bytes:
    stored = await octomate.conversations.get(conversation.id)
    assert stored.transcript_file_id is not None
    return await octomate.files.read(stored.transcript_file_id, owner_id=user.id)


@pytest.mark.parametrize("agent_id", ["claude", "codex", "deepseek", "inkling"])
async def test_uploads_append_where_the_kept_bytes_end(
    agent_id: Literal["claude", "codex", "deepseek", "inkling"],
) -> None:
    async with a_driven_turn(agent_id=agent_id) as (
        client,
        octomate,
        conversation,
        user,
    ):
        url = f"/hooks/{agent_id}/transcripts/{conversation.id}"

        asked = await client.post(f"{url}/offsets", json=[], headers=AUTH)
        assert asked.status_code == 200
        assert asked.json() == {"offsets": {str(conversation.id): 0}, "subagents": {}}

        first = await client.post(
            url,
            params={"offset": 0, "transcript_id": str(conversation.id)},
            content=FIRST,
            headers=AUTH,
        )
        assert first.status_code == 200
        assert TranscriptKept.model_validate(first.json()).size == len(FIRST)

        second = await client.post(
            url,
            params={"offset": len(FIRST), "transcript_id": str(conversation.id)},
            content=SECOND,
            headers=AUTH,
        )
        assert TranscriptKept.model_validate(second.json()).size == len(FIRST + SECOND)
        offsets = await client.post(f"{url}/offsets", json=[], headers=AUTH)
        assert offsets.json() == {
            "offsets": {str(conversation.id): len(FIRST + SECOND)},
            "subagents": {},
        }
        assert await kept_bytes(octomate, conversation, user) == FIRST + SECOND


@pytest.mark.parametrize("subagent_id", ["", "child"])
async def test_bytes_that_do_not_continue_the_kept_ones_are_refused(
    subagent_id: str,
) -> None:
    async with a_driven_turn() as (client, octomate, conversation, user):
        url = f"/hooks/claude/transcripts/{conversation.id}"
        binding = await client.post(
            f"{url}/offsets", json=[subagent_id] if subagent_id else [], headers=AUTH
        )
        target = (
            next(iter(binding.json()["subagents"]))
            if subagent_id
            else str(conversation.id)
        )
        await client.post(
            url,
            params={"offset": 0, "transcript_id": target},
            content=FIRST,
            headers=AUTH,
        )

        for offset in (0, len(FIRST) + 1):
            refused = await client.post(
                url,
                params={"offset": offset, "transcript_id": target},
                content=SECOND,
                headers=AUTH,
            )
            assert refused.status_code == 409
            # The refusal says where to resume.
            assert TranscriptKept.model_validate(refused.json()).size == len(FIRST)

        partial = await client.post(
            url,
            params={"offset": len(FIRST), "transcript_id": target},
            content=b'{"type":"assis',
            headers=AUTH,
        )
        assert partial.status_code == 422
        if subagent_id:
            [conversation] = await octomate.conversations.subagents(conversation.id)
        assert await kept_bytes(octomate, conversation, user) == FIRST


@pytest.mark.parametrize("existing", [False, True], ids=["first-upload", "append"])
async def test_overlapping_uploads_keep_each_byte_once(
    monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    async with a_driven_turn() as (client, octomate, conversation, user):
        url = f"/hooks/claude/transcripts/{conversation.id}"
        target = {"transcript_id": str(conversation.id)}
        prefix, data = (FIRST, SECOND) if existing else (b"", FIRST)
        if prefix:
            response = await client.post(
                url, params={"offset": 0, **target}, content=prefix, headers=AUTH
            )
            assert response.status_code == 200
        barrier = asyncio.Barrier(2)
        store = octomate.conversations.store_transcript

        async def store_after_both_arrive(
            uploaded: UploadFile,
            start: int,
            *,
            conversation: Conversation,
            files: FileManager,
            owner_id: UUID7,
        ) -> FileVariant:
            await barrier.wait()
            return await store(
                uploaded,
                start,
                conversation=conversation,
                files=files,
                owner_id=owner_id,
            )

        monkeypatch.setattr(
            octomate.conversations, "store_transcript", store_after_both_arrive
        )
        async with (
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(
                    app=octomate, raise_app_exceptions=False
                ),
                base_url="http://core",
            ) as concurrent,
            asyncio.timeout(5),
        ):
            responses = await asyncio.gather(
                *(
                    concurrent.post(
                        url,
                        params={"offset": len(prefix), **target},
                        content=data,
                        headers=AUTH,
                    )
                    for _ in range(2)
                )
            )
        assert await kept_bytes(octomate, conversation, user) == prefix + data
        assert sorted(response.status_code for response in responses) == [200, 409]
        for response in responses:
            assert TranscriptKept.model_validate(response.json()).size == len(
                prefix + data
            )


@pytest.mark.parametrize("subagent_id", ["", "child"])
async def test_only_the_owner_uploads_and_only_while_the_turn_runs(
    subagent_id: str,
) -> None:
    async with a_driven_turn() as (client, octomate, conversation, user):
        url = f"/hooks/claude/transcripts/{conversation.id}"
        target = str(uuid7() if subagent_id else conversation.id)
        stranger = await a_user("mo")
        await a_api_key(stranger, "another-hook-secret")

        params = {"offset": 0, "transcript_id": target}
        unsigned = await client.post(url, params=params, content=FIRST)
        assert unsigned.status_code == 401
        foreign = await client.post(
            url,
            params=params,
            content=FIRST,
            headers={"Authorization": "Bearer another-hook-secret"},
        )
        assert foreign.status_code == 404

        octomate.habitats.residences[user.id].conversations.clear()
        late = await client.post(url, params=params, content=FIRST, headers=AUTH)
        assert late.status_code == 404
        offsets = await client.post(f"{url}/offsets", json=[], headers=AUTH)
        assert offsets.status_code == 404
        stored = await octomate.conversations.get(conversation.id)
        assert stored.transcript_file_id is None
        assert await octomate.conversations.subagents(conversation.id) == []


async def test_a_subagent_upload_cannot_target_another_parents_child() -> None:
    async with a_driven_turn() as (client, octomate, conversation, user):
        other = await octomate.conversations.ensure(
            conversation.thread_id,
            agent_tentacle_id="claude",
            subagent_id="commission",
            parent_conversation_id=conversation.id,
        )
        child = await octomate.conversations.ensure(
            conversation.thread_id,
            agent_tentacle_id="claude",
            subagent_id="child",
            parent_conversation_id=other.id,
        )
        octomate.habitats.residences[user.id].conversations[other.id] += 1
        accepted = await client.post(
            f"/hooks/claude/transcripts/{other.id}",
            params={"offset": 0, "transcript_id": str(child.id)},
            content=FIRST,
            headers=AUTH,
        )
        assert accepted.status_code == 200
        offsets = await client.post(
            f"/hooks/claude/transcripts/{other.id}/offsets", json=[], headers=AUTH
        )
        assert offsets.json() == {
            "offsets": {str(other.id): 0, str(child.id): len(FIRST)},
            "subagents": {str(child.id): "child"},
        }
        parent_offsets = await client.post(
            f"/hooks/claude/transcripts/{conversation.id}/offsets",
            json=[],
            headers=AUTH,
        )
        assert parent_offsets.json() == {
            "offsets": {str(conversation.id): 0, str(other.id): 0},
            "subagents": {str(other.id): "commission"},
        }
        refused = await client.post(
            f"/hooks/claude/transcripts/{conversation.id}",
            params={"offset": len(FIRST), "transcript_id": str(child.id)},
            content=SECOND,
            headers=AUTH,
        )
        assert refused.status_code == 404
        for key, status in (("", 422), ("child", 422), (str(uuid7()), 404)):
            invalid = await client.post(
                f"/hooks/claude/transcripts/{conversation.id}",
                params={"offset": 0, "transcript_id": key},
                content=SECOND,
                headers=AUTH,
            )
            assert invalid.status_code == status
        assert await kept_bytes(octomate, child, user) == FIRST


async def test_upload_requires_configured_auth() -> None:
    async with a_driven_turn() as (client, octomate, conversation, _):
        octomate.auth = None
        response = await client.post(
            f"/hooks/claude/transcripts/{conversation.id}",
            params={"offset": 0, "transcript_id": str(conversation.id)},
            content=FIRST,
            headers=AUTH,
        )
        assert response.status_code == 503
        stored = await octomate.conversations.get(conversation.id)
        assert stored.transcript_file_id is None


async def test_the_runtime_key_uploads() -> None:
    async with a_driven_turn() as (client, octomate, conversation, user):
        url = f"/hooks/claude/transcripts/{conversation.id}"
        issued = await octomate.agents["claude"].runtime_api_key(user.id)
        assert issued is not None
        assert issued.key.scopes == ["hooks", "mcp"]

        runtime = await client.post(
            url,
            params={"offset": 0, "transcript_id": str(conversation.id)},
            content=FIRST,
            headers={"Authorization": f"Bearer {issued.token.get_secret_value()}"},
        )
        assert runtime.status_code == 200
        assert await kept_bytes(octomate, conversation, user) == FIRST


async def test_download_serves_the_owners_transcript_after_the_turn() -> None:
    async with a_driven_turn() as (client, core, conversation, _):
        url = f"/hooks/claude/transcripts/{conversation.id}"
        assert (await client.get(url, headers=AUTH)).status_code == 404
        posted = await client.post(
            url,
            params={"offset": 0, "transcript_id": str(conversation.id)},
            content=FIRST,
            headers=AUTH,
        )
        assert posted.status_code == 200
        core.habitats.residences.clear()

        for _ in range(2):
            response = await client.get(url, headers=AUTH)
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/jsonl"
            assert response.content == FIRST
        await a_api_key(await a_user("other"), "other-hook")
        assert (
            await client.get(url, headers={"Authorization": "Bearer other-hook"})
        ).status_code == 404
        assert (await client.get(url)).status_code == 401
        assert (
            await client.get(f"/hooks/claude/transcripts/{uuid7()}", headers=AUTH)
        ).status_code == 404


@pytest.mark.parametrize("agent_id", ["claude", "codex", "deepseek"])
async def test_upload_requires_hooks_scope(
    agent_id: Literal["claude", "codex", "deepseek"],
) -> None:
    async with a_driven_turn(agent_id=agent_id) as (
        client,
        octomate,
        conversation,
        user,
    ):
        url = f"/hooks/{agent_id}/transcripts/{conversation.id}"
        await a_api_key(user, "a-hook-only-secret", scopes=["hooks"])
        hook = await client.post(
            url,
            params={"offset": 0, "transcript_id": str(conversation.id)},
            content=FIRST,
            headers={"Authorization": "Bearer a-hook-only-secret"},
        )
        assert hook.status_code == 200

        await a_api_key(user, "an-mcp-only-secret", scopes=["mcp"])
        mcp = await client.post(
            url,
            params={"offset": len(FIRST), "transcript_id": str(conversation.id)},
            content=SECOND,
            headers={"Authorization": "Bearer an-mcp-only-secret"},
        )
        assert mcp.status_code == 401
        assert await kept_bytes(octomate, conversation, user) == FIRST


@pytest.mark.parametrize("agent_id", ["claude", "codex", "deepseek"])
async def test_upload_route_requires_its_connected_harness(agent_id: str) -> None:
    octomate = Octomate(
        config=OctomateConfig(auth=auth_config(), habitat=SbxHabitatConfig(image=IMAGE))
    )
    user = await a_user()
    await a_api_key(user, SECRET.get_secret_value())
    conversation = await octomate.conversations.ensure(
        await a_thread(), agent_tentacle_id=agent_id
    )
    scope = InvocationScope(user_id=user.id, conversation_id=conversation.id)
    octomate.habitats.residence(scope).conversations[conversation.id] += 1
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=octomate), base_url="http://core"
    ) as client:
        uploaded = await client.post(
            f"/hooks/{agent_id}/transcripts/{conversation.id}",
            params={"offset": 0, "transcript_id": str(conversation.id)},
            content=FIRST,
            headers=AUTH,
        )
    assert uploaded.status_code == 404
    stored = await octomate.conversations.get(conversation.id)
    assert stored.transcript_file_id is None


async def test_a_tentacle_refuses_another_harness_conversation() -> None:
    async with a_driven_turn() as (client, octomate, conversation, user):
        other = await octomate.conversations.ensure(
            conversation.thread_id, agent_tentacle_id="codex"
        )
        scope = InvocationScope(user_id=user.id, conversation_id=other.id)
        octomate.habitats.residence(scope).conversations[other.id] += 1
        refused = await client.post(
            f"/hooks/claude/transcripts/{other.id}",
            params={"offset": 0, "transcript_id": str(conversation.id)},
            content=FIRST,
            headers=AUTH,
        )
        assert refused.status_code == 404
        stored = await octomate.conversations.get(other.id)
        assert stored.transcript_file_id is None


@pytest.mark.parametrize("agent_id", ["codex", "deepseek"])
async def test_the_cli_sends_new_bytes_without_the_sdks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_id: Literal["codex", "deepseek"],
) -> None:
    transcript = tmp_path / "session.jsonl"
    secret = SECRET.get_secret_value()
    async with (
        a_driven_turn(agent_id=agent_id) as (_, octomate, conversation, user),
        served(octomate) as base,
    ):
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
        monkeypatch.setitem(sys.modules, "openai_codex", None)
        transcript.write_bytes(FIRST)
        first = await cli_upload(base, conversation, transcript, secret)
        assert first.exit_code == 0, first.output
        assert first.stdout == ""
        assert await kept_bytes(octomate, conversation, user) == FIRST

        # The line still being written stays behind.
        transcript.write_bytes(FIRST + SECOND + b'{"type":"cost-st')
        second = await cli_upload(base, conversation, transcript, secret)
        assert second.exit_code == 0, second.output
        assert second.stdout == ""
        assert await kept_bytes(octomate, conversation, user) == FIRST + SECOND

        again = await cli_upload(base, conversation, transcript, secret)
        assert again.exit_code == 0, again.output
        assert again.stdout == ""
        assert await kept_bytes(octomate, conversation, user) == FIRST + SECOND


async def test_claude_hook_passes_its_runtime_path_to_the_uploader(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "runtime-transcript.jsonl"
    transcript.write_bytes(FIRST)
    async with (
        a_driven_turn() as (_, octomate, conversation, user),
        served(octomate) as base,
    ):
        cli_settings.cache_clear()
        result = await asyncio.to_thread(
            CliRunner().invoke,
            app,
            ["claude", "upload", "--conversation", str(conversation.id)],
            input=json.dumps(
                {
                    "hook_event_name": "Stop",
                    "session_id": str(uuid7()),
                    "transcript_path": str(transcript),
                }
            ),
            env={
                CLISettings.env("url"): base,
                CLISettings.env("token"): SECRET.get_secret_value(),
            },
        )
        assert result.exit_code == 0, result.output
        assert result.stdout == ""
        assert await kept_bytes(octomate, conversation, user) == FIRST


@pytest.mark.parametrize("hook_event", ["Stop", "SessionEnd"])
async def test_claude_hooks_keep_subagent_files_separately(
    tmp_path: Path, hook_event: Literal["Stop", "SessionEnd"]
) -> None:
    transcript = tmp_path / "runtime-transcript.jsonl"
    transcript.write_bytes(FIRST)
    children = transcript.with_suffix("") / "subagents"
    children.mkdir(parents=True)
    first = children / "agent-first.jsonl"
    second = children / "agent-second.jsonl"
    final_line = b'{"type":"assistant","uuid":"late-answer"}\n'
    first.write_bytes(FIRST + final_line[:-2])
    second.write_bytes(SECOND)
    async with (
        a_driven_turn() as (_, octomate, conversation, user),
        served(octomate) as base,
    ):
        stopped = await cli_upload(
            base,
            conversation,
            transcript,
            SECRET.get_secret_value(),
            hook_event="SubagentStop",
            child=first,
        )
        assert stopped.exit_code == 0, stopped.output
        [early] = await octomate.conversations.subagents(conversation.id)
        assert early.subagent_id == "first"
        assert await kept_bytes(octomate, early, user) == FIRST
        assert (
            await octomate.conversations.get(conversation.id)
        ).transcript_file_id is None

        uploaded = await cli_upload(
            base,
            conversation,
            transcript,
            SECRET.get_secret_value(),
            hook_event=hook_event,
        )
        assert uploaded.exit_code == 0, uploaded.output
        assert uploaded.stdout == ""
        child_conversations = {
            child.subagent_id: child
            for child in await octomate.conversations.subagents(conversation.id)
        }
        assert set(child_conversations) == {"first", "second"}
        assert await kept_bytes(octomate, conversation, user) == FIRST
        assert await kept_bytes(octomate, child_conversations["first"], user) == FIRST
        assert await kept_bytes(octomate, child_conversations["second"], user) == SECOND
        parent = await octomate.conversations.get(conversation.id)
        file_ids = {child.transcript_file_id for child in child_conversations.values()}
        assert len({parent.transcript_file_id, *file_ids}) == 3

        # A later hook picks up the child's completed line and the parent's new turn.
        first.write_bytes(FIRST + final_line)
        transcript.write_bytes(FIRST + SECOND)
        for _ in range(2):
            uploaded = await cli_upload(
                base,
                conversation,
                transcript,
                SECRET.get_secret_value(),
                hook_event=hook_event,
            )
            assert uploaded.exit_code == 0, uploaded.output
            assert uploaded.stdout == ""
        assert await kept_bytes(octomate, conversation, user) == FIRST + SECOND
        assert (
            await kept_bytes(octomate, child_conversations["first"], user)
            == FIRST + final_line
        )
        assert await kept_bytes(octomate, child_conversations["second"], user) == SECOND
        retained = await octomate.conversations.subagents(conversation.id)
        assert {child.id for child in retained} == {
            child.id for child in child_conversations.values()
        }
        assert {child.transcript_file_id for child in retained} == file_ids


async def test_cli_uploads_independent_files_concurrently(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(FIRST)
    children = transcript.with_suffix("") / "subagents"
    children.mkdir(parents=True)
    (children / "agent-child.jsonl").write_bytes(SECOND)
    handshakes = 0
    uploads: list[tuple[str, int, bytes]] = []
    together = asyncio.Barrier(2)
    async with a_driven_turn() as (_, octomate, conversation, user):

        @octomate.middleware("http")
        async def barrier(
            request: Request, call_next: RequestResponseEndpoint
        ) -> Response:
            nonlocal handshakes
            assert request.method == "POST"
            if request.url.path.endswith("/offsets"):
                handshakes += 1
            else:
                uploads.append(
                    (
                        request.query_params["transcript_id"],
                        int(request.query_params["offset"]),
                        await request.body(),
                    )
                )
                if handshakes == 1:
                    await asyncio.wait_for(together.wait(), 3)
            return await call_next(request)

        async with served(octomate) as base:
            result = await cli_upload(
                base, conversation, transcript, SECRET.get_secret_value()
            )
            assert result.exit_code == 0, result.output
            assert handshakes == 1
            [child] = await octomate.conversations.subagents(conversation.id)
            assert set(uploads) == {
                (str(conversation.id), 0, FIRST),
                (str(child.id), 0, SECOND),
            }
            uploads.clear()
            transcript.write_bytes(FIRST + SECOND + b"partial")
            for _ in range(2):
                result = await cli_upload(
                    base, conversation, transcript, SECRET.get_secret_value()
                )
                assert result.exit_code == 0, result.output
            assert handshakes == 3
            assert uploads == [(str(conversation.id), len(FIRST), SECOND)]
        [child] = await octomate.conversations.subagents(conversation.id)
        assert await kept_bytes(octomate, child, user) == SECOND
        assert await kept_bytes(octomate, conversation, user) == FIRST + SECOND


def test_hook_upload_requires_the_runtime_path() -> None:
    result = CliRunner().invoke(
        app,
        ["claude", "upload", "--conversation", str(uuid7())],
        input=json.dumps({"hook_event_name": "Stop", "session_id": str(uuid7())}),
    )
    assert result.exit_code == 2
    assert "hook input requires transcript_path" in result.output


async def test_the_cli_fails_when_the_upload_is_refused(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(FIRST)
    async with (
        a_driven_turn() as (_, octomate, conversation, user),
        served(octomate) as base,
    ):
        stranger = await a_user("mo")
        await a_api_key(stranger, "another-hook-secret")

        foreign = await cli_upload(
            base, conversation, transcript, "another-hook-secret"
        )
        assert foreign.exit_code == 1
        assert "404" in foreign.output
        assert "No active habitat invocation" in foreign.output
        stored = await octomate.conversations.get(conversation.id)
        assert stored.transcript_file_id is None

        # A file shorter than the kept bytes is not the conversation's transcript.
        secret = SECRET.get_secret_value()
        await cli_upload(base, conversation, transcript, secret)
        shorter = tmp_path / "other.jsonl"
        shorter.write_bytes(FIRST[:8])
        wrong = await cli_upload(base, conversation, shorter, secret)
        assert wrong.exit_code == 1
        assert str(shorter) in wrong.output
        assert await kept_bytes(octomate, conversation, user) == FIRST


def test_the_cli_needs_an_address_and_a_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No config file in either scope: the developer's own cli.toml must not fill in.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(CLISettings.env("url"), raising=False)
    monkeypatch.delenv(CLISettings.env("token"), raising=False)

    result = CliRunner().invoke(
        app,
        [
            "codex",
            "upload",
            "--conversation",
            str(uuid7()),
            "--path",
            "session.jsonl",
        ],
    )
    assert result.exit_code == 2
    assert CLISettings.env("token") in result.output
