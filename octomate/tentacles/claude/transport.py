from __future__ import annotations

import asyncio
import json
import logging
import shlex
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk._internal.transport import Transport

logger = logging.getLogger(__name__)


class SshTransport(Transport):
    """Transport that runs Claude Code CLI on a remote host via SSH.

    Spawns ``ssh <host> claude code --output-format stream-json ...`` and
    communicates over stdin/stdout JSON-lines, exactly like the default
    subprocess transport but through an SSH connection.
    """

    host: str
    cwd: str
    identity_file: str | None
    ssh_options: list[str]
    _process: asyncio.subprocess.Process | None
    _read_task: asyncio.Task | None
    _messages: asyncio.Queue[dict[str, Any]]

    def __init__(
        self,
        host: str,
        cwd: str = ".",
        identity_file: str | None = None,
        ssh_options: list[str] | None = None,
    ) -> None:
        self.host = host
        self.cwd = cwd
        self.identity_file = identity_file
        self.ssh_options = ssh_options or []
        self._process = None
        self._read_task = None
        self._messages = asyncio.Queue()

    def _build_ssh_command(self) -> list[str]:
        cmd = ["ssh"]
        # Keep the connection alive and fail fast on forward failures.
        cmd += ["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
        if self.identity_file:
            cmd += ["-i", self.identity_file]
        cmd += self.ssh_options
        cmd.append(self.host)
        # The remote command: claude code in streaming JSON mode.
        remote = (
            f"cd {shlex.quote(self.cwd)} && "
            "claude code --output-format stream-json --verbose --input-format stream-json"
        )
        cmd.append(remote)
        return cmd

    async def connect(self) -> None:
        cmd = self._build_ssh_command()
        logger.info("SshTransport: connecting via %s", " ".join(cmd[:4]))
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._read_task = asyncio.create_task(self._read_loop())
        # Drain stderr to logger in the background.
        asyncio.create_task(self._stderr_loop())

    async def _read_loop(self) -> None:
        assert self._process and self._process.stdout
        while True:
            line = await self._process.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
                await self._messages.put(msg)
            except json.JSONDecodeError:
                logger.debug("SshTransport: non-JSON line: %s", text[:200])

    async def _stderr_loop(self) -> None:
        assert self._process and self._process.stderr
        while True:
            line = await self._process.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.debug("SshTransport stderr: %s", text)

    async def write(self, data: str) -> None:
        if not self._process or not self._process.stdin:
            raise ConnectionError("SshTransport: not connected")
        self._process.stdin.write(data.encode("utf-8"))
        await self._process.stdin.drain()

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            try:
                msg = await self._messages.get()
                yield msg
            except asyncio.CancelledError:
                break

    async def close(self) -> None:
        if self._read_task:
            self._read_task.cancel()
            self._read_task = None
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except (TimeoutError, ProcessLookupError):
                self._process.kill()
            self._process = None

    def is_ready(self) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
            and self._process.stdin is not None
        )

    async def end_input(self) -> None:
        if self._process and self._process.stdin:
            self._process.stdin.close()
            await self._process.stdin.wait_closed()
