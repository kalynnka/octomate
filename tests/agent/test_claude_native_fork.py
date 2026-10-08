"""A native Claude Code session's bytes, kept as its transcript streams in, and the
fork that lays them where a landed thread's workspace files sessions, for a session
Octomate drives to resume."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from octomate_cli.tentacles.claude import CLAUDE_STREAM_PATH
from octomate_protocol.stream import StreamEof, StreamLine
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.websockets import WebSocketDisconnect

from octomate import Octomate
from octomate.config import ClaudeCodeConfig, OctomateConfig
from octomate.schemas.conversation import Conversation
from octomate.schemas.thread import CLAUDE_NATIVE_ID, ThreadKey
from octomate.schemas.user import UserProfile
from octomate.tentacles.claude import ClaudeCodeTentacle
from octomate.tentacles.claude import base as claude_base
from octomate.tentacles.claude.transcript import transcripts_dir
from tests.agent.test_claude_stream import AUTH, CLIENT_PATH, frames, hello_json
from tests.agent.test_claude_stream import stream_client as a_stream_client
from tests.agent.test_claude_tailer import (
    SESSION_ID,
    SESSION_KEY,
    TURN_ONE,
    TURN_TWO,
    line_bytes,
)
from tests.support.users import a_user, auth_config

WHOLE = b"".join(line_bytes(record) for record in TURN_ONE + TURN_TWO)


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


async def a_native_session() -> tuple[Octomate, ClaudeCodeTentacle, UserProfile]:
    octomate = Octomate(config=OctomateConfig(auth=auth_config()))
    tentacle = octomate.connect(
        ClaudeCodeTentacle("claude", octomate, config=ClaudeCodeConfig())
    )
    user = await a_user()
    return octomate, tentacle, UserProfile(channel_user_id="lu", user_id=user.id)


async def kept_bytes(
    octomate: Octomate, conversation: Conversation, owner_id: uuid.UUID
) -> bytes:
    stored = await octomate.conversations.get(conversation.id)
    assert stored.transcript_file_id is not None
    return await octomate.files.read(stored.transcript_file_id, owner_id=owner_id)


async def test_a_native_session_keeps_its_own_bytes_across_a_reconnect() -> None:
    """A reconnect resends the turn it dropped in; what is kept already is skipped,
    and a line past a gap is never kept, so the file stays the session's own."""
    octomate, tentacle, sender = await a_native_session()
    assert sender.user_id is not None
    state, _ = await tentacle.session_tailer.attach_remote(
        SESSION_ID, CLIENT_PATH, sender
    )
    conversation = state.conversation
    assert conversation is not None
    framed = frames(TURN_ONE + TURN_TWO)
    kept = 0
    for _, start, end, line in framed[: len(TURN_ONE) + 1]:
        kept = await tentacle.keep_transcript(
            conversation,
            StreamLine(start=start, end=end, line=line),
            kept,
            owner_id=sender.user_id,
        )
    # The reconnect starts over from the end of the last committed turn.
    for _, start, end, line in framed[len(TURN_ONE) :]:
        kept = await tentacle.keep_transcript(
            conversation,
            StreamLine(start=start, end=end, line=line),
            kept,
            owner_id=sender.user_id,
        )
    past_a_gap = StreamLine(start=kept + 5, end=kept + 7, line="{")
    assert (
        await tentacle.keep_transcript(
            conversation, past_a_gap, kept, owner_id=sender.user_id
        )
        == kept
    )

    assert kept == len(WHOLE)
    assert await kept_bytes(octomate, conversation, sender.user_id) == WHOLE


def test_lines_over_the_socket_are_kept_for_their_owner() -> None:
    client, tentacle = a_stream_client()
    with client:
        with client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            websocket.receive_text()  # welcome
            for agent_id, start, end, line in frames(TURN_ONE + TURN_TWO):
                websocket.send_text(
                    StreamLine(
                        agent_id=agent_id, start=start, end=end, line=line
                    ).model_dump_json()
                )
            websocket.send_text(StreamEof().model_dump_json())
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()

        async def kept() -> bytes:
            octomate = tentacle.octomate
            thread = await octomate.thread_manager.get(SESSION_KEY, with_messages=False)
            owner = await octomate.users.native_profile(CLAUDE_NATIVE_ID, "lu")
            assert thread is not None
            assert owner is not None
            assert owner.user_id is not None
            [conversation] = await octomate.conversations.for_thread(thread.id)
            return await kept_bytes(octomate, conversation, owner.user_id)

        assert client.portal is not None
        assert client.portal.call(kept) == WHOLE


async def test_a_native_session_forks_into_a_session_this_tentacle_drives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The kept history up to the last whole turn is laid where the landed
    thread's workspace files sessions and forked there; the staged copy goes."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    octomate, tentacle, sender = await a_native_session()
    assert sender.user_id is not None
    tailer = tentacle.session_tailer
    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, sender)
    conversation = state.conversation
    assert conversation is not None
    kept = 0
    for agent_id, start, end, line in frames(TURN_ONE + TURN_TWO):
        await tailer.feed_remote(state, agent_id, line, start, end)
        kept = await tentacle.keep_transcript(
            conversation,
            StreamLine(start=start, end=end, line=line),
            kept,
            owner_id=sender.user_id,
        )
    await tailer.finish_remote(state)
    source = await octomate.conversations.get(conversation.id)
    forked_from: list[tuple[str, str, bytes]] = []

    def fork_session(session_id: str, directory: str) -> SimpleNamespace:
        staged = transcripts_dir(Path(directory)) / f"{session_id}.jsonl"
        forked_from.append((session_id, directory, staged.read_bytes()))
        return SimpleNamespace(session_id="forked-session")

    monkeypatch.setattr(claude_base, "fork_session", fork_session)
    # The model the session's turns ran, as a transcript records it.
    tentacle.models = {"anthropic:claude-opus-4-8": "claude-opus-4-8"}
    await tentacle.validate_fork(source, sender=sender)

    landed = await tentacle.fork(
        source, ThreadKey("trunkline", "thread", "lu", "landing"), sender=sender
    )

    [(session_id, directory, staged)] = forked_from
    assert session_id == SESSION_ID
    assert staged == WHOLE
    assert directory == str(octomate.workspaces.open(landed.id, None).path)
    assert not (transcripts_dir(Path(directory)) / f"{SESSION_ID}.jsonl").exists()
    [copy] = await octomate.conversations.for_thread(landed.id)
    assert copy.agent_tentacle_id == "claude"
    assert copy.external_id == "forked-session"


async def test_a_session_on_a_model_this_server_does_not_offer_cannot_fork() -> None:
    """Refused while nothing has moved: resumed here, the session could only run
    on a model its history was never written on."""
    octomate, tentacle, sender = await a_native_session()
    assert sender.user_id is not None
    tailer = tentacle.session_tailer
    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, sender)
    conversation = state.conversation
    assert conversation is not None
    kept = 0
    for agent_id, start, end, line in frames(TURN_ONE + TURN_TWO):
        await tailer.feed_remote(state, agent_id, line, start, end)
        kept = await tentacle.keep_transcript(
            conversation,
            StreamLine(start=start, end=end, line=line),
            kept,
            owner_id=sender.user_id,
        )
    await tailer.finish_remote(state)
    source = await octomate.conversations.get(conversation.id)
    tentacle.models = {"anthropic:claude-sonnet-5-5": "claude-sonnet-5-5"}

    with pytest.raises(ValueError, match="'claude-opus-4-8', which 'claude' does not"):
        await tentacle.validate_fork(source, sender=sender)


async def test_a_native_session_with_nothing_kept_cannot_fork() -> None:
    octomate, tentacle, sender = await a_native_session()
    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    source = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=tentacle.native_id
    )

    with pytest.raises(ValueError, match="has not been uploaded"):
        await tentacle.validate_fork(source, sender=sender)
