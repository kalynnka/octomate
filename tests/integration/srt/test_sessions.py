"""Session operations in SRT use owner storage and need no model credentials."""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import pytest
from claude_agent_sdk import get_session_messages, project_key_for_directory
from httpx2 import URL
from openai_codex import CodexConfig
from openai_codex.api import ApprovalMode, Sandbox
from uuid_utils.compat import uuid7

from octomate.config.agents import CodexConfig as AgentCodexConfig
from octomate.habitat.srt import SrtHabitat
from octomate.tentacles.claude.ink import ClaudeInk
from octomate.tentacles.codex.client import SharedCodex
from octomate.tentacles.codex.ink import CodexInk
from tests.integration.native_sandbox.provider import ScriptedProvider, local_provider


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


@pytest.mark.timeout(90)
async def test_installed_codex_tools_and_fresh_process_resume(
    habitat: SrtHabitat, tmp_path: Path
) -> None:
    executable = os.environ.get("SRT_CODEX_EXECUTABLE")
    if executable is None:
        pytest.skip("Set SRT_CODEX_EXECUTABLE to a native Codex binary")
    binary = Path(await anyio.Path(executable).resolve())
    await habitat.stop()
    habitat.config = habitat.config.model_copy(
        update={"read_paths": [binary.parent.parent]}
    )
    await habitat.prepare()
    home = tmp_path / "config" / ".codex"
    home.mkdir()
    state = ScriptedProvider("responses")
    handler = AsyncMock(return_value={"decision": "decline"})
    with local_provider(state) as endpoint:
        config = CodexConfig(
            codex_bin=str(binary),
            cwd=str(tmp_path),
            env={"CODEX_HOME": str(home), "NO_PROXY": "", "no_proxy": ""},
            config_overrides=(
                'model_provider="audit"',
                'model="gpt-5"',
                "model_providers.audit="
                + json.dumps(
                    {
                        "name": "audit",
                        "base_url": endpoint + "/v1",
                        "wire_api": "responses",
                        "requires_openai_auth": False,
                    }
                ).replace(": ", " = "),
            ),
        )
        ink = CodexInk(
            AgentCodexConfig(runtime=config),
            agent_id="codex",
            mcp_url=URL("http://127.0.0.1:9000/mcp"),
            handler=handler,
        )
        session_id = None
        for command in ("printf srt-proof > proof.txt; cat proof.txt", "cat proof.txt"):
            state.tool = "exec_command"
            state.arguments = {"cmd": command, "max_output_tokens": 1000}
            state.call_id = str(uuid7())
            state.outcome = None
            async with SharedCodex(
                config, handler, instrument=False, habitat=habitat
            ) as client:
                assert client.metadata.userAgent
                if session_id is None:
                    thread, _ = await ink.start_thread(
                        client=client,
                        cwd=str(tmp_path),
                        sandbox=Sandbox.workspace_write,
                        approval_mode=ApprovalMode.deny_all,
                        base_instructions=None,
                        config={},
                        developer_instructions=None,
                        model=None,
                        model_provider=None,
                        personality=None,
                        ephemeral=False,
                    )
                else:
                    thread, _ = await ink.resume_thread(
                        client=client,
                        cwd=str(tmp_path),
                        sandbox=Sandbox.workspace_write,
                        approval_mode=ApprovalMode.deny_all,
                        base_instructions=None,
                        config={},
                        developer_instructions=None,
                        model=None,
                        model_provider=None,
                        personality=None,
                        thread_id=session_id,
                    )
                await thread.run("Execute the scripted probe once.")
                assert state.outcome is not None
                assert "srt-proof" in state.outcome.text
                assert (tmp_path / "proof.txt").read_text() == "srt-proof"
                assert session_id is None or thread.id == session_id
                session_id = thread.id
            assert not habitat.processes
            await habitat.stop()
            await habitat.prepare()
    handler.assert_not_called()
