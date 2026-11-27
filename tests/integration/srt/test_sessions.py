"""Session operations in SRT use owner storage and need no model credentials."""

import json
import os
from pathlib import Path

import anyio
import pytest
from claude_agent_sdk import get_session_messages, project_key_for_directory
from uuid_utils.compat import uuid7

from octomate.habitat.srt import SrtHabitat
from octomate.tentacles.claude.ink import ClaudeInk


async def test_claude_import_and_fork_preserve_owner_storage(
    habitat: SrtHabitat, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = os.environ.get("SRT_CLI_ENV")
    if installed is None:
        pytest.skip(
            "Set SRT_CLI_ENV to a standalone CLI virtualenv with the Claude extra"
        )
    prefix = Path(await anyio.Path(installed).resolve())
    python = prefix / "bin" / "python"
    runtime = Path(await anyio.Path(python).resolve()).parent.parent
    await habitat.stop()
    habitat.config = habitat.config.model_copy(update={"read_paths": [prefix, runtime]})
    await habitat.prepare()
    result = await habitat.run(
        [
            str(python),
            "-c",
            "import importlib.util; assert importlib.util.find_spec('octomate') is None",
        ],
        workdir=str(tmp_path),
    )
    assert result.exit_code == 0, result.stderr
    config = tmp_path / "config" / ".claude"
    env = {
        "CLAUDE_CONFIG_DIR": str(config),
        "PATH": str(prefix / "bin") + os.pathsep + habitat.environment()["PATH"],
    }
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    session = str(uuid7())
    row = {
        "type": "user",
        "uuid": str(uuid7()),
        "sessionId": session,
        "parentUuid": None,
        "message": {"role": "user", "content": "retained history"},
    }
    core_config = tmp_path.parent / f"unused-core-{uuid7()}"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(core_config))
    ink = ClaudeInk()
    imported = await ink.fork_session(
        session,
        cwd=source,
        habitat=habitat,
        env=env,
        transcript=(json.dumps(row) + "\n").encode(),
    )
    forked = await ink.fork_session(
        imported,
        cwd=destination,
        habitat=habitat,
        env=env,
    )
    await ink.relocate_session(forked, cwd=destination, habitat=habitat)
    assert len({session, imported, forked}) == 3
    assert not core_config.exists()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    for session_id, directory in ((imported, source), (forked, destination)):
        path = config / "projects" / project_key_for_directory(directory)
        assert (path / f"{session_id}.jsonl").is_file()
        [message] = get_session_messages(session_id, directory=str(directory))
        assert message.message == row["message"]
        assert message.uuid != row["uuid"]
