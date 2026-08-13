"""The client half of the transcript stream: framing, cursor discipline, the
per-session single-instance lock, and what `main` refuses to run without."""

from __future__ import annotations

import fcntl
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

import octomate_cli.tail as tail_mod
import pytest
from octomate_cli.config import HOOK_SECRET_ENV
from octomate_cli.tail import FileCursor, SessionTail, main


def test_read_lines_frames_complete_lines_and_holds_the_fragment(
    tmp_path: Path,
) -> None:
    path = tmp_path / "s.jsonl"
    path.write_bytes(b'{"a":1}\n{"b":2}\npartial')
    cursor = FileCursor(path)

    assert cursor.read_lines() == [(0, 8, b'{"a":1}'), (8, 16, b'{"b":2}')]
    assert cursor.offset == 16
    assert cursor.read_lines() == []  # the fragment stays unread, whole

    with path.open("ab") as handle:
        handle.write(b" done\n")
    assert cursor.read_lines() == [(16, 29, b"partial done")]


def test_blank_lines_ship_too(tmp_path: Path) -> None:
    """The server advances its expected offset from every frame; a skipped blank
    would read as a gap there."""
    path = tmp_path / "s.jsonl"
    path.write_bytes(b'{"a":1}\n\n{"b":2}\n')
    assert FileCursor(path).read_lines() == [
        (0, 8, b'{"a":1}'),
        (8, 9, b""),
        (9, 17, b'{"b":2}'),
    ]


def test_a_missing_file_yields_nothing_and_truncation_resets(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    cursor = FileCursor(path)
    assert cursor.read_lines() == []

    path.write_bytes(b'{"a":1}\n')
    assert cursor.read_lines() == [(0, 8, b'{"a":1}')]
    path.write_bytes(b'{"c":3}\n')  # shorter-or-equal rewrite: not append-only
    cursor.offset = 100
    assert cursor.read_lines() == [(0, 8, b'{"c":3}')]


def test_session_tail_discovers_subagent_files_and_seeds_server_offsets(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "sess-1.jsonl"
    transcript.write_bytes(b"")
    subagents = tmp_path / "sess-1" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-abc.jsonl").write_bytes(b"")

    tail = SessionTail("sess-1", transcript, offsets={"abc": 5})
    assert tail.discover() == ["", "abc"]
    assert tail.cursor("abc").path == subagents / "agent-abc.jsonl"
    assert tail.cursor("abc").offset == 5  # the server's welcome named the resume
    assert tail.cursor("").offset == 0


def test_a_spooled_codex_child_ships_keyed_by_its_basename(tmp_path: Path) -> None:
    """Codex writes a child rollout as a sibling file only its content identifies, so
    the launcher spools the exact paths the SubagentStop hooks name; the tail ships
    each under its basename and leaves classification to the server."""
    transcript = tmp_path / "rollout-parent.jsonl"
    child = tmp_path / "rollout-child.jsonl"
    spool = tmp_path / "session.paths"
    spool.write_text(f"{child}\n")

    tail = SessionTail("sess-1", transcript, spool=spool)
    assert tail.discover() == ["", "rollout-child.jsonl"]
    assert tail.cursor("rollout-child.jsonl").path == child

    tail = SessionTail("sess-1", transcript, spool=tmp_path / "absent.paths")
    assert tail.discover() == [""]  # no spool yet: just the session's own file


async def test_main_refuses_to_run_without_the_hook_credential(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.delenv(HOOK_SECRET_ENV, raising=False)
    # And no config file in either scope: the developer's real cli.toml must not
    # fill it in.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        main(
            session_id="s1",
            transcript_path=tmp_path / "t.jsonl",
            url="ws://127.0.0.1:1/hooks/claude/stream",
            cwd="",
        )
    assert HOOK_SECRET_ENV in capfd.readouterr().err


def test_a_second_tail_for_the_same_session_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launcher fires on every prompt; the flock is what makes each spawn after
    the first exit quietly, with no stale-pidfile state to manage."""
    monkeypatch.setenv(HOOK_SECRET_ENV, "s")
    monkeypatch.setattr(tail_mod, "LOCK_GRACE", 0.0)  # a held lock cedes instantly
    streamed: list[str] = []

    async def record_only(
        url: str,
        session_id: str,
        transcript_path: Path,
        cwd: str,
        secret: str,
        spool: Path | None = None,
    ) -> None:
        streamed.append(session_id)

    monkeypatch.setattr(tail_mod, "run_tail", record_only)
    session_id = f"lock-{uuid4()}"
    lock_path = Path(tempfile.gettempdir()) / f"octomate-tail-{session_id}.lock"

    with lock_path.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main(
            session_id=session_id,
            transcript_path=tmp_path / "t.jsonl",
            url="ws://127.0.0.1:1/hooks/claude/stream",
            cwd="",
        )
    assert streamed == []  # the held lock made it a no-op

    main(
        session_id=session_id,
        transcript_path=tmp_path / "t.jsonl",
        url="ws://127.0.0.1:1/hooks/claude/stream",
        cwd="",
    )
    assert streamed == [session_id]  # lock released with the holder: a fresh tail runs


def test_a_spawn_during_the_drain_waits_out_the_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The server relays `finalize` at `Stop`, so a queued prompt's launcher can fire
    while the previous turn's tail is still draining out. The grace window bridges
    that overlap: the spawn waits for the lock instead of ceding the round."""
    monkeypatch.setenv(HOOK_SECRET_ENV, "s")
    monkeypatch.setattr(tail_mod, "LOCK_GRACE", 5.0)
    monkeypatch.setattr(tail_mod, "LOCK_POLL", 0.05)
    streamed: list[str] = []

    async def record_only(
        url: str,
        session_id: str,
        transcript_path: Path,
        cwd: str,
        secret: str,
        spool: Path | None = None,
    ) -> None:
        streamed.append(session_id)

    monkeypatch.setattr(tail_mod, "run_tail", record_only)
    session_id = f"grace-{uuid4()}"
    lock_path = Path(tempfile.gettempdir()) / f"octomate-tail-{session_id}.lock"

    held = lock_path.open("w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    release = threading.Timer(0.3, held.close)  # closing the handle drops the flock
    release.start()
    try:
        main(
            session_id=session_id,
            transcript_path=tmp_path / "t.jsonl",
            url="ws://127.0.0.1:1/hooks/claude/stream",
            cwd="",
        )
    finally:
        release.cancel()
        if not held.closed:
            held.close()
    assert streamed == [session_id]


def test_a_spool_handoff_reaches_a_running_tail_and_defers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Codex SubagentStop fires while a tail already holds the session: that
    invocation's whole job is appending the child's path — the holder re-reads the
    spool on every pump — and then getting out of the way at the lock."""
    monkeypatch.setenv(HOOK_SECRET_ENV, "s")
    monkeypatch.setattr(tail_mod, "LOCK_GRACE", 0.0)
    monkeypatch.setattr(
        tail_mod, "run_tail", None
    )  # any call would TypeError: nothing may stream here
    session_id = f"spool-{uuid4()}"
    spool = tmp_path / "session.paths"
    child = tmp_path / "rollout-child.jsonl"
    lock_path = Path(tempfile.gettempdir()) / f"octomate-tail-{session_id}.lock"

    with lock_path.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main(
            session_id=session_id,
            transcript_path=tmp_path / "rollout-parent.jsonl",
            url="ws://127.0.0.1:1/hooks/codex/stream",
            cwd="",
            spool=spool,
            agent_path=child,
        )
    assert spool.read_text() == f"{child}\n"
