"""The plugin reaches the configured MCP and keeps the native hook contract."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from octomate_cli.tentacles.codex.hooks import (
    HANDLED_HOOK_EVENTS,
    HOOK_TIMEOUT,
    LAUNCHER_HOOK_EVENTS,
)

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "octomate"


def test_plugin_bundles_the_existing_hook_events_and_mcp_command() -> None:
    hooks = json.loads((PLUGIN / "hooks/hooks.json").read_text())["hooks"]
    assert set(hooks) == set(HANDLED_HOOK_EVENTS)
    for event, groups in hooks.items():
        [group] = groups
        handlers = group["hooks"]
        assert handlers[0] == {
            "type": "command",
            "command": "octomate-emit --path /hooks/codex",
            "timeout": HOOK_TIMEOUT,
        }
        if event in LAUNCHER_HOOK_EVENTS:
            assert handlers[1:] == [
                {
                    "type": "command",
                    "command": "octomate-launch --path /hooks/codex --agent codex --octomate octomate",
                    "timeout": HOOK_TIMEOUT,
                }
            ]
        else:
            assert len(handlers) == 1
    assert json.loads((PLUGIN / ".mcp.json").read_text()) == {
        "mcpServers": {
            "octomate": {
                "command": "octomate-codex-mcp",
                "env_vars": ["OCTOMATE_CLI_URL", "OCTOMATE_CLI_TOKEN"],
            }
        }
    }


@pytest.mark.parametrize("command", ["octomate-emit", "octomate-launch"])
def test_portable_hook_commands_keep_the_script_cli(command: str) -> None:
    result = subprocess.run(
        [str(Path(sys.executable).parent / command), "--invalid"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("from_environment", [False, True])
@pytest.mark.parametrize("agent", ["codex", "claude"])
async def test_plugin_mcp_preserves_instructions_tools_and_caller(
    tmp_path: Path,
    from_environment: bool,
    agent: str,
) -> None:
    instructions = "Search the MCP servers proxied by Octomate."
    upstream = FastMCP("test-octomate", instructions=instructions)
    callers: list[tuple[str, str]] = []
    ready = asyncio.Event()

    async def notify_started() -> None:
        ready.set()

    @upstream.tool
    def echo(text: str) -> str:
        headers = get_http_headers(include={"authorization"})
        callers.append((headers["authorization"], headers["x-octomate-client"]))
        return text

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                upstream.http_app(path="/octomate/mcp"),
                lifespan="on",
                log_config=None,
                access_log=False,
                callback_notify=notify_started,
            )
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await asyncio.wait_for(ready.wait(), timeout=10)
            config = tmp_path / ".octomate/cli.toml"
            config.parent.mkdir()
            config.write_text(
                f'url = "http://127.0.0.1:{port}"\ntoken = "plugin-test-token"\n'
            )
            parameters = StdioServerParameters(
                command=str(Path(sys.executable).parent / f"octomate-{agent}-mcp"),
                cwd=str(tmp_path),
                env={
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("OCTOMATE")
                },
            )
            if from_environment:
                parameters.env = {
                    **(parameters.env or {}),
                    "OCTOMATE_CLI_URL": f"http://127.0.0.1:{port}",
                    "OCTOMATE_CLI_TOKEN": "environment-token",
                }
                config.write_text('url = "http://127.0.0.1:1"\ntoken = "wrong-token"\n')
            async with asyncio.timeout(15):
                async with (
                    stdio_client(parameters) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    initialized = await session.initialize()
                    assert initialized.instructions == instructions
                    assert [
                        tool.name for tool in (await session.list_tools()).tools
                    ] == ["echo"]
                    result = await session.call_tool("echo", {"text": "through-plugin"})
                    assert not result.is_error
                    assert result.structured_content == {"result": "through-plugin"}
            token = "environment-token" if from_environment else "plugin-test-token"
            assert callers == [(f"Bearer {token}", f"{agent}-native")]
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=10)
