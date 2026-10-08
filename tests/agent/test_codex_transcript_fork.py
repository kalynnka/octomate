"""Owner-scoped native transcript imports into independent Codex conversations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import opendal
import pytest
from arcanus.materia.sqlalchemy import AsyncSession
from fastapi import UploadFile
from openai_codex import CodexConfig as RuntimeConfig
from pydantic_ai.messages import ModelRequest, UserPromptPart
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.agents import CodexConfig
from octomate.database import async_session
from octomate.managers.files import FileManager
from octomate.schemas.conversation import Conversation
from octomate.schemas.files import File
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.thread import CODEX_NATIVE_ID, ThreadKey
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.codex.transcript import TurnPermissions
from octomate.types.json import JsonObject
from tests.support.managers import a_thread
from tests.support.users import a_user


@dataclass
class ForkCase:
    tentacle: CodexTentacle
    source: Conversation
    target: Conversation
    owner_id: UUID
    prefix: bytes
    pending: bytes
    home: Path
    fork: AsyncMock


@pytest.fixture
async def case(
    in_memory_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ForkCase:
    octomate = Octomate(
        files=FileManager(
            storage=opendal.AsyncOperator("fs", root=str(tmp_path / "files"))
        )
    )
    home = tmp_path / "codex"
    tentacle = CodexTentacle(
        "codex",
        octomate,
        config=CodexConfig(runtime=RuntimeConfig(env={"CODEX_HOME": str(home)})),
        commands=octomate.commands,
        projects=octomate.projects,
        threads=octomate.threads,
        files=octomate.files,
        conversations=octomate.conversations,
        deferred_actions=octomate.deferred_actions,
        workspaces=octomate.workspaces,
        users=octomate.users,
        bearers=octomate.bearers,
        auth=octomate.auth,
        gateway_manager=octomate.gateway,
    )
    # The model the native session ran, which its fork resumes on.
    tentacle.models = {"openai:gpt-6-luna": "gpt-6-luna"}
    owner = await a_user()
    source_thread = await octomate.threads.ensure(
        ThreadKey(CODEX_NATIVE_ID, "thread", str(owner.id), uuid7().hex)
    )
    source = await octomate.conversations.ensure(
        source_thread.id, agent_tentacle_id=CODEX_NATIVE_ID
    )
    target = await octomate.conversations.ensure(
        await a_thread("target"), agent_tentacle_id=tentacle.id
    )
    session_id = str(uuid7())
    completed_id = str(uuid7())
    prefix = (
        json.dumps(
            {
                "timestamp": "2026-09-29T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": session_id, "session_id": session_id},
            }
        ).encode()
        + b"\n"
        + json.dumps(
            {
                "timestamp": "2026-09-29T00:00:00Z",
                "type": "turn_context",
                "payload": {
                    "model": "gpt-6-luna",
                    "effort": "high",
                    "approval_policy": "on-request",
                    "approvals_reviewer": "auto_review",
                    "sandbox_policy": {"type": "workspace-write"},
                },
            }
        ).encode()
        + b"\n"
        + json.dumps(
            {
                "timestamp": "2026-09-29T00:00:01Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": completed_id},
            }
        ).encode()
        + b"\n"
    )
    pending = (
        b'{"type":"event_msg","payload":{"type":"task_started"}}\n'
        + json.dumps(
            {
                "timestamp": "2026-09-29T00:00:02Z",
                "type": "turn_context",
                "payload": {
                    "approval_policy": "never",
                    "sandbox_policy": {"type": "danger-full-access"},
                },
            }
        ).encode()
        + b"\n"
    )
    for label, end in (
        ("completed", len(prefix)),
        ("not uploaded", len(prefix) + 1000),
        ("in flight", None),
    ):
        await octomate.conversations.record_external_run(
            source,
            completed_id if label == "completed" else str(uuid7()),
            [ModelRequest(parts=[UserPromptPart(label)])],
            native_session_id=session_id,
            model_name="gpt-6-luna" if label == "completed" else "other-model",
            permission_mode="auto_review" if label == "completed" else "full_access",
            end_offset=end,
        )
    await octomate.conversations.store_transcript(
        UploadFile(BytesIO(prefix + pending), filename="rollout.jsonl"),
        0,
        conversation=source,
        files=octomate.files,
        owner_id=owner.id,
    )
    source = await octomate.conversations.get(source.id)
    fork = AsyncMock(return_value=str(uuid7()))
    monkeypatch.setattr(tentacle, "fork_session", fork)
    return ForkCase(tentacle, source, target, owner.id, prefix, pending, home, fork)


@pytest.mark.parametrize("partial_tail", [b"", b'{"type":"event_msg","payload":'])
async def test_fork_copies_only_completed_uploaded_history(
    case: ForkCase, tmp_path: Path, partial_tail: bytes
) -> None:
    tentacle = case.tentacle
    files = tentacle.octomate.files
    assert case.source.transcript_file_id is not None
    await files.append(
        case.source.transcript_file_id,
        BytesIO(partial_tail),
        offset=len(case.prefix + case.pending),
        owner_id=case.owner_id,
    )
    result = await tentacle.fork_transcript(
        case.source,
        case.target,
        owner_id=case.owner_id,
        cwd=tmp_path,
    )
    assert result.external_id == case.fork.return_value
    assert result.permission_mode == "auto_review"
    assert result.runs[-1].model_name == "gpt-6-luna"
    assert result.runs[-1].permission_mode == "auto_review"
    assert result.transcript_file_id is not None
    assert result.transcript_file_id != case.source.transcript_file_id
    assert len(result.messages) == 1
    [part] = result.messages[0].parts
    assert isinstance(part, UserPromptPart)
    assert part.content == "completed"
    assert (
        await files.read(result.transcript_file_id, owner_id=case.owner_id)
        == case.prefix
    )
    [imported] = case.home.glob(f"sessions/users/{case.owner_id.hex}/*.jsonl")
    opening, remainder = imported.read_bytes().split(b"\n", 1)
    import_id = json.loads(opening)["payload"]["id"]
    assert import_id not in {case.source.external_id, result.external_id}
    assert UUID(import_id).version == 7
    assert remainder == (case.prefix + case.pending).split(b"\n", 1)[1]
    assert imported.stat().st_mode & 0o777 == 0o600
    assert imported.parent.stat().st_mode & 0o777 == 0o700
    assert case.fork.call_args.args[0].external_id == import_id
    completed_id = json.loads(case.prefix.splitlines()[-1])["payload"]["turn_id"]
    assert case.fork.call_args.kwargs == {
        "cwd": tmp_path,
        "last_turn_id": completed_id,
    }
    assert case.target.external_id is None
    assert case.target.transcript_file_id is None
    assert case.target.permission_mode is None
    assert (
        await tentacle.octomate.conversations.get(case.source.id)
    ).permission_mode is None
    assert case.source.transcript_file_id is not None
    await files.append(
        case.source.transcript_file_id,
        BytesIO(b"later\n"),
        offset=len(case.prefix + case.pending + partial_tail),
        owner_id=case.owner_id,
    )
    assert (
        await files.read(result.transcript_file_id, owner_id=case.owner_id)
        == case.prefix
    )
    assert imported.read_bytes() == opening + b"\n" + remainder
    assert (
        await tentacle.octomate.conversations.get(case.source.id)
    ).external_id == case.source.external_id


async def test_two_destinations_have_distinct_imports(
    case: ForkCase, tmp_path: Path
) -> None:
    first = await case.tentacle.fork_transcript(
        case.source,
        case.target,
        owner_id=case.owner_id,
        cwd=tmp_path,
    )
    target = await case.tentacle.octomate.conversations.ensure(
        await a_thread("another"), agent_tentacle_id="codex"
    )
    case.fork.return_value = str(uuid7())
    second = await case.tentacle.fork_transcript(
        case.source,
        target,
        owner_id=case.owner_id,
        cwd=tmp_path,
    )
    assert first.external_id != second.external_id
    assert first.transcript_file_id != second.transcript_file_id
    assert len(list(case.home.glob("sessions/users/*/*.jsonl"))) == 2
    assert (
        case.fork.call_args_list[0].args[0].external_id
        != case.fork.call_args_list[1].args[0].external_id
    )


async def test_another_owner_cannot_import_the_transcript(
    case: ForkCase, tmp_path: Path
) -> None:
    other = await a_user("other")
    with pytest.raises(FileNotFoundError):
        await case.tentacle.fork_transcript(
            case.source,
            case.target,
            owner_id=other.id,
            cwd=tmp_path,
        )
    case.fork.assert_not_awaited()
    assert not case.home.exists()


@pytest.mark.parametrize("failure", ["fork", "commit"])
async def test_fork_failure_publishes_no_target_or_file(
    case: ForkCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "fork":
        case.fork.side_effect = RuntimeError("fork failed")
    else:
        monkeypatch.setattr(
            AsyncSession, "commit", AsyncMock(side_effect=RuntimeError("commit failed"))
        )
    with pytest.raises(RuntimeError, match=r"fork failed|commit failed"):
        await case.tentacle.fork_transcript(
            case.source,
            case.target,
            owner_id=case.owner_id,
            cwd=tmp_path,
        )
    target = await case.tentacle.octomate.conversations.get(case.target.id)
    assert target.external_id is None
    assert target.transcript_file_id is None
    assert target.permission_mode is None
    assert not target.messages
    assert not list(case.home.rglob("*.jsonl"))
    assert len(list((tmp_path / "files").glob("users/*/*"))) == 1
    async with async_session() as session:
        assert await session.count(File) == 1


async def test_no_completed_uploaded_turn_cannot_fork(
    case: ForkCase, tmp_path: Path
) -> None:
    async with async_session() as session:
        runs = await session.list(
            ExternalAgentRun,
            expressions=[ExternalAgentRun["conversation_id"] == case.source.id],
            limit=None,
            order_bys=[],
        )
        for run in runs:
            run.end_offset = None
        await session.commit()
    with pytest.raises(ValueError, match="No completed Codex turn"):
        await case.tentacle.fork_transcript(
            case.source, case.target, owner_id=case.owner_id, cwd=tmp_path
        )
    case.fork.assert_not_awaited()
    assert not case.home.exists()


@pytest.mark.parametrize(
    ("policy", "reviewer", "sandbox", "expected"),
    [
        ("on-request", "user", "workspace-write", "user_review"),
        ("on-request", "auto_review", "workspace-write", "auto_review"),
        ("never", "user", "danger-full-access", "full_access"),
    ],
)
def test_native_permission_presets(
    policy: str, reviewer: str, sandbox: str, expected: str
) -> None:
    permissions = TurnPermissions.model_validate(
        {
            "approval_policy": policy,
            "approvals_reviewer": reviewer,
            "sandbox_policy": {"type": sandbox},
        }
    )
    assert permissions.permission_mode == expected


@pytest.mark.parametrize(
    "sandbox",
    [
        {"type": "read-only"},
        {"type": "workspace-write", "network_access": True},
        {"type": "workspace-write", "writable_roots": ["/laptop/private"]},
        {"type": "workspace-write", "exclude_slash_tmp": True},
    ],
)
def test_custom_native_permissions_are_not_silently_replaced(
    sandbox: JsonObject,
) -> None:
    with pytest.raises(ValueError, match="sandbox_policy"):
        TurnPermissions.model_validate(
            {
                "approval_policy": "on-request",
                "approvals_reviewer": "user",
                "sandbox_policy": sandbox,
            }
        )


async def test_missing_native_permissions_does_not_use_server_defaults(
    case: ForkCase, tmp_path: Path
) -> None:
    async with async_session() as session:
        run = await session.one(
            ExternalAgentRun,
            expressions=[ExternalAgentRun["end_offset"] == len(case.prefix)],
        )
        run.permission_mode = None
        await session.commit()
    with pytest.raises(ValueError, match="no supported permission preset"):
        await case.tentacle.fork_transcript(
            case.source, case.target, owner_id=case.owner_id, cwd=tmp_path
        )
    case.fork.assert_not_awaited()
    assert not case.home.exists()


@pytest.mark.parametrize("previous_completed", [False, True])
@pytest.mark.parametrize(
    "event",
    ["task_complete", "turn_aborted", "user_message", "wrong_turn", "wrong_record"],
)
async def test_fork_requires_a_matching_terminal_event(
    case: ForkCase, tmp_path: Path, event: str, previous_completed: bool
) -> None:
    conversations = case.tentacle.octomate.conversations
    files = case.tentacle.octomate.files
    run_id = str(uuid7())
    closing = (
        json.dumps(
            {
                "timestamp": "2026-09-29T00:00:02Z",
                "type": "response_item" if event == "wrong_record" else "event_msg",
                "payload": {
                    "type": "task_complete" if event.startswith("wrong_") else event,
                    "turn_id": str(uuid7()) if event == "wrong_turn" else run_id,
                },
            }
        ).encode()
        + b"\n"
    )
    content = case.prefix + case.pending + closing
    assert case.source.external_id is not None
    assert case.source.transcript_file_id is not None
    await files.append(
        case.source.transcript_file_id,
        BytesIO(closing),
        offset=len(case.prefix + case.pending),
        owner_id=case.owner_id,
    )
    if not previous_completed:
        async with async_session() as session:
            runs = await session.list(
                ExternalAgentRun,
                expressions=[ExternalAgentRun["conversation_id"] == case.source.id],
                limit=None,
                order_bys=[],
            )
            for run in runs:
                run.end_offset = None
            await session.commit()
    await conversations.record_external_run(
        case.source,
        run_id,
        [ModelRequest(parts=[UserPromptPart("later turn")])],
        native_session_id=case.source.external_id,
        model_name="other-model",
        permission_mode="full_access",
        end_offset=len(content),
    )
    terminal = event in {"task_complete", "turn_aborted"}
    if not terminal and not previous_completed:
        with pytest.raises(ValueError, match="No completed Codex turn"):
            await case.tentacle.fork_transcript(
                case.source, case.target, owner_id=case.owner_id, cwd=tmp_path
            )
        case.fork.assert_not_awaited()
        assert not case.home.exists()
        return
    result = await case.tentacle.fork_transcript(
        case.source, case.target, owner_id=case.owner_id, cwd=tmp_path
    )
    assert result.transcript_file_id is not None
    assert await files.read(result.transcript_file_id, owner_id=case.owner_id) == (
        content if terminal else case.prefix
    )
    assert len(result.messages) == int(previous_completed) + int(terminal)
    assert result.runs[-1].model_name == ("other-model" if terminal else "gpt-6-luna")
    assert result.permission_mode == ("full_access" if terminal else "auto_review")
    completed_id = json.loads(case.prefix.splitlines()[-1])["payload"]["turn_id"]
    assert case.fork.call_args.kwargs["last_turn_id"] == (
        run_id if terminal else completed_id
    )


async def test_latest_completed_turn_does_not_validate_stale_older_offsets(
    case: ForkCase,
) -> None:
    source = case.source.model_copy(
        update={
            "runs": [
                ExternalAgentRun(
                    id=str(uuid7()),
                    conversation_id=case.source.id,
                    native_session_id=case.source.external_id,
                    model_name="other-model",
                    permission_mode="full_access",
                    end_offset=len(case.prefix) - 1,
                ),
                *case.source.runs,
            ]
        }
    )
    data, completed = await case.tentacle.read_fork_transcript(
        source, owner_id=case.owner_id
    )
    completed_id = json.loads(case.prefix.splitlines()[-1])["payload"]["turn_id"]
    assert completed.id == completed_id
    assert completed.end_offset == len(case.prefix)
    assert completed.permission_mode == "auto_review"
    assert data == case.prefix + case.pending


async def test_fork_rejects_an_offset_inside_a_line(
    case: ForkCase, tmp_path: Path
) -> None:
    async with async_session() as session:
        run = await session.one(
            ExternalAgentRun,
            expressions=[
                ExternalAgentRun["conversation_id"] == case.source.id,
                ExternalAgentRun["end_offset"] == len(case.prefix),
            ],
        )
        run.end_offset = len(case.prefix) - 1
        await session.commit()
    with pytest.raises(ValueError, match="line boundary"):
        await case.tentacle.fork_transcript(
            case.source, case.target, owner_id=case.owner_id, cwd=tmp_path
        )
    case.fork.assert_not_awaited()
    assert not case.home.exists()


@pytest.mark.parametrize("invalid", ["boundary", "identity", "ancestor"])
async def test_invalid_import_fails_without_creating_a_rollout(
    case: ForkCase, invalid: str
) -> None:
    data = case.prefix
    if invalid == "boundary":
        data = data[:-1]
    elif invalid == "identity":
        data = data.replace(
            str(case.source.external_id).encode(), str(uuid7()).encode()
        )
    else:
        data = data.replace(
            b'"payload": {', b'"payload": {"history_base": {"thread_id":"missing"},', 1
        )
    assert case.source.external_id is not None
    with pytest.raises(
        ValueError, match=r"line boundary|source session|ancestor files"
    ):
        async with case.tentacle.import_transcript(
            data,
            external_id=case.source.external_id,
            owner_id=case.owner_id,
        ):
            pytest.fail("Invalid history must not reach Codex")
    assert not case.home.exists()
