"""Claude files a session's transcript under the cwd it ran in and resumes only from
there — from anywhere else the CLI exits with "No conversation found". A run that
changes directory mid-thread carries the transcript there first, subagents
included, found by session id: a moved teleport forks the conversation, so the
directory it came from is not on record.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from octomate.tentacles.agents.claude.transcript import (
    relocate_session,
    transcripts_dir,
)

# A chat workspace this deployment ran a session in, and the directory Claude
# filed that session under — every character outside [A-Za-z0-9] dashed.
OBSERVED_CWD = Path(
    "/Users/luhui/Projects/octoverse/inky/.octomate/workspaces/chat/"
    "01a058d6-3a45-7150-b54c-6275f12d4ef3"
)
OBSERVED_DIR = (
    "-Users-luhui-Projects-octoverse-inky--octomate-workspaces-chat-"
    "01a058d6-3a45-7150-b54c-6275f12d4ef3"
)


def test_the_transcripts_dir_is_the_cwd_with_every_other_character_dashed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

    assert transcripts_dir(OBSERVED_CWD) == tmp_path / "projects" / OBSERVED_DIR


def test_relocating_moves_the_transcript_and_its_subagents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    old, new = tmp_path / "chat", tmp_path / "project"
    source = transcripts_dir(old)
    (source / "sid" / "subagents").mkdir(parents=True)
    (source / "sid.jsonl").write_text("{}\n")
    (source / "sid" / "subagents" / "agent-1.jsonl").write_text("{}\n")

    relocate_session("sid", cwd=new)

    target = transcripts_dir(new)
    assert (target / "sid.jsonl").read_text() == "{}\n"
    assert (target / "sid" / "subagents" / "agent-1.jsonl").is_file()
    assert not (source / "sid.jsonl").exists()
    assert not (source / "sid").exists()


def test_a_session_with_no_transcript_where_it_ran_cannot_move(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

    with pytest.raises(FileNotFoundError, match="has 0 transcripts"):
        relocate_session("sid", cwd=tmp_path / "b")
