from __future__ import annotations

import asyncio
import json
import logging
import shlex
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk._internal.transport import Transport
from claude_agent_sdk._version import __version__ as SDK_VERSION

logger = logging.getLogger(__name__)

MAX_BUFFER_SIZE = 1024 * 1024  # 1 MB, same as the default transport


class SSHTransport(Transport):
    """Transport that runs Claude Code CLI on a remote host via SSH.

    Spawns ``ssh <host> claude code --output-format stream-json ...`` and
    communicates over stdin/stdout JSON-lines, exactly like the default
    subprocess transport but through an SSH connection.
    """

    host: str
    cwd: str
    identity_file: str | None
    ssh_options: list[str]
    env: dict[str, str]
    _process: asyncio.subprocess.Process | None
    _ready: bool
    _stderr_lines: list[str]

    def __init__(
        self,
        host: str,
        cwd: str = ".",
        identity_file: str | None = None,
        ssh_options: list[str] | None = None,
        env: dict[str, str] | None = None,
        claude_bin: str = "claude",
    ) -> None:
        self.host = host
        self.cwd = cwd
        self.identity_file = identity_file
        self.ssh_options = ssh_options or []
        self.claude_bin = claude_bin
        self.env = env or {}
        self._process = None
        self._ready = False
        self._stderr_lines = []

    def _build_ssh_command(self) -> list[str]:
        cmd = [
            "ssh",
            "-T",  # disable TTY allocation — essential for piped JSON
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if self.identity_file:
            cmd += ["-i", self.identity_file]
        cmd += self.ssh_options
        cmd.append(self.host)

        # Build env exports for the remote shell.  The default SDK transport
        # sets these locally; we must pass them over SSH.
        env_vars = {
            "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
            "CLAUDE_AGENT_SDK_VERSION": SDK_VERSION,
        }
        env_vars.update(self.env)
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env_vars.items())

        # Expand leading ~ so the remote shell resolves it correctly.
        # shlex.quote would wrap it in single quotes and prevent expansion.
        cwd = self.cwd
        if cwd.startswith("~"):
            cwd_expr = '"$HOME"' + shlex.quote(cwd[1:])
        else:
            cwd_expr = shlex.quote(cwd)

        remote = (
            f"cd {cwd_expr} && "
            f"export {exports} && "
            f"{self.claude_bin} code --output-format stream-json --verbose --input-format stream-json"
        )
        cmd.append(remote)
        return cmd

    async def connect(self) -> None:
        cmd = self._build_ssh_command()
        logger.info("SshTransport: connecting via %s", " ".join(cmd[:6]))
        self._stderr_lines = []
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(self._stderr_loop())
        self._ready = True

    async def _stderr_loop(self) -> None:
        assert self._process and self._process.stderr
        while True:
            line = await self._process.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                self._stderr_lines.append(text)
                logger.debug("SshTransport stderr: %s", text)

    async def write(self, data: str) -> None:
        if not self._ready or not self._process or not self._process.stdin:
            raise ConnectionError("SshTransport: not connected")
        self._process.stdin.write(data.encode("utf-8"))
        await self._process.stdin.drain()

    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        """Return an async iterator that yields parsed JSON messages.

        Must be a *sync* method (not ``async def``) because the SDK calls it
        without ``await``.  The returned async generator handles the actual
        async I/O internally.
        """
        return self._read_messages_impl()

    async def _read_messages_impl(self) -> AsyncIterator[dict[str, Any]]:
        if not self._process or not self._process.stdout:
            raise ConnectionError("SshTransport: not connected")

        json_buffer = ""
        while True:
            line = await self._process.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            # Accumulate partial JSON, same strategy as the default transport.
            # SSH or long messages may split a single JSON object across lines.
            json_buffer += text

            if len(json_buffer) > MAX_BUFFER_SIZE:
                logger.error(
                    "SshTransport: JSON buffer exceeded %d bytes, resetting",
                    MAX_BUFFER_SIZE,
                )
                json_buffer = ""
                continue

            try:
                data = json.loads(json_buffer)
                json_buffer = ""
                yield data
            except json.JSONDecodeError:
                # Not yet a complete JSON object — keep accumulating.
                continue

        # Process has exited.
        returncode = await self._process.wait()
        if returncode and returncode != 0:
            stderr_tail = "\n".join(self._stderr_lines[-20:])
            raise ConnectionError(
                f"SshTransport: remote process exited with code {returncode}\n"
                f"stderr:\n{stderr_tail}"
            )

    async def close(self) -> None:
        self._ready = False
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except (TimeoutError, ProcessLookupError):
                self._process.kill()
            self._process = None

    def is_ready(self) -> bool:
        return (
            self._ready
            and self._process is not None
            and self._process.returncode is None
        )

    async def end_input(self) -> None:
        if self._process and self._process.stdin:
            self._process.stdin.close()
            await self._process.stdin.wait_closed()
