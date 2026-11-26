"""Offline Claude SDK forks land in the destination VM's session storage."""

import json
import os
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from claude_agent_sdk import get_session_messages, project_key_for_directory
from uuid_utils.compat import uuid7

from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import HabitatManager
from octomate.nerve import Nerve
from octomate.schemas.habitat import InvocationScope
from octomate.tentacles.claude.ink import ClaudeInk
from tests.integration.sbx import remove_sandbox
from tests.support.managers import a_workspace_manager


@pytest.mark.timeout(600)
async def test_retained_and_guest_session_forks_land_in_the_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sbx_cleanup: AsyncExitStack
) -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    if not image:
        pytest.fail("Set SBX_TEST_IMAGE to the immutable SDK image reference")
    manager = HabitatManager(
        SbxHabitatConfig(image=image), Nerve(), a_workspace_manager()
    )
    scope = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    sandbox = manager.residence(scope).habitat
    assert isinstance(sandbox, SbxHabitat)
    session = str(uuid7())
    row = {
        "type": "user",
        "uuid": str(uuid7()),
        "sessionId": session,
        "parentUuid": None,
        "message": {"role": "user", "content": "retained history"},
    }
    transcript = (json.dumps(row) + "\n").encode()
    config = sandbox.config_dir / ".claude"
    source, destination = sandbox.root / "source", sandbox.root / "destination"
    source.mkdir(parents=True)
    destination.mkdir()
    core_config = tmp_path / "unused-core-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(core_config))
    ink = ClaudeInk()
    try:
        async with manager.enter(scope, uuid7()):
            imported = await ink.fork_session(
                session,
                cwd=source,
                habitat=sandbox,
                env={"CLAUDE_CONFIG_DIR": str(config)},
                transcript=transcript,
            )
            moved = await ink.fork_session(
                imported,
                cwd=destination,
                habitat=sandbox,
                env={"CLAUDE_CONFIG_DIR": str(config)},
            )
            await ink.relocate_session(moved, cwd=destination, habitat=sandbox)
        assert len({session, imported, moved}) == 3
        assert not core_config.exists()
        assert not list(config.rglob(f"{session}.jsonl"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
        for session_id, directory in ((imported, source), (moved, destination)):
            path = config / "projects" / project_key_for_directory(directory)
            assert (path / f"{session_id}.jsonl").is_file()
            [message] = get_session_messages(session_id, directory=str(directory))
            assert message.message == row["message"]
            assert message.uuid != row["uuid"]
    finally:
        if sandbox.managed:
            sbx_cleanup.push_async_callback(remove_sandbox, sandbox)
        sbx_cleanup.push_async_callback(manager.close)
