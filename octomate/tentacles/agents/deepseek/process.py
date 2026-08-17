"""One `dsh web` child: spawn it, confirm its URL from the banner, kill it with us.

The server it starts has no TLS and no auth, which is only acceptable because
it is bound to loopback and owned by this process — the bind host is fixed at
`127.0.0.1` and deliberately not configurable. The port is the configured one,
fixed rather than ephemeral, and that is load-bearing: it is the address the
tentacle probes before starting anything, so a fixed port is what lets the
next octomate — or any other dsh client — attach to this harness instead of
starting a second writer of the same `$DSH_HOME`, which dsh does not lock and
which corrupts session logs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import HttpUrl

logger = logging.getLogger(__name__)

# The banner `dsh web` prints once its `/api` route owner is mounted. Upstream
# treats the line as a readiness contract, not a courtesy: it is held until the
# plugin tree settles, so a supervisor may RPC the moment it sees it. Waiting
# for it is how we know the server is ready, and the URL it carries is the one
# the server actually bound.
BANNER = re.compile(r"dsh web:\s*(https?://\S+)")

STOP_ESCALATE_SECONDS = 5.0


@dataclass
class DeepseekProcess:
    executable: str
    port: int
    extra_args: list[str]
    dsh_home: Path
    ready_timeout: float
    process: asyncio.subprocess.Process | None = field(default=None, init=False)
    relays: list[asyncio.Task[None]] = field(default_factory=list, init=False)

    async def start(self) -> HttpUrl:
        """Spawn `dsh web` and return its base URL once the banner lands.

        DSH_HOME is always the config's `dsh_home` — the config collects it,
        from the environment or dsh's own default, so nothing is decided or
        overridden here. A dsh that exits or stays silent past `ready_timeout`
        fails the start rather than being retried: a broken install is a
        broken install.
        """
        process = await asyncio.create_subprocess_exec(
            self.executable,
            "web",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            *self.extra_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "DSH_HOME": str(self.dsh_home)},
        )
        self.process = process
        stdout, stderr = process.stdout, process.stderr
        if stdout is None or stderr is None:
            raise RuntimeError("dsh web subprocess was spawned without pipes")
        # Drain stderr from the first byte: a chatty dsh must not fill the pipe
        # and stall behind a banner nobody can read.
        self.relays = [asyncio.create_task(self.relay(stderr, logging.WARNING))]
        try:
            base_url = await asyncio.wait_for(
                self.read_banner(process, stdout), self.ready_timeout
            )
        except TimeoutError:
            await self.stop()
            raise RuntimeError(
                f"dsh web did not report its URL within {self.ready_timeout:g}s"
            ) from None
        except BaseException:
            await self.stop()
            raise
        self.relays.append(asyncio.create_task(self.relay(stdout, logging.INFO)))
        logger.info("dsh ready at %s", base_url)
        return base_url

    @staticmethod
    async def read_banner(
        process: asyncio.subprocess.Process, stdout: asyncio.StreamReader
    ) -> HttpUrl:
        while line_bytes := await stdout.readline():
            line = line_bytes.decode(errors="replace").rstrip()
            if line:
                logger.info("dsh: %s", line)
            match = BANNER.search(line)
            if match is not None:
                return HttpUrl(match.group(1))
        code = await process.wait()
        raise RuntimeError(f"dsh web exited before reporting a URL (code {code})")

    @staticmethod
    async def relay(stream: asyncio.StreamReader, level: int) -> None:
        while line_bytes := await stream.readline():
            line = line_bytes.decode(errors="replace").rstrip()
            if line:
                logger.log(level, "dsh: %s", line)

    async def stop(self) -> None:
        """SIGTERM, escalating to SIGKILL — a dsh that ignores the term still
        must not outlive octomate. A SIGKILLed octomate does orphan the child
        (dsh has no parent-liveness flag); that residual risk is documented,
        not solved here."""
        for task in self.relays:
            task.cancel()
        for task in self.relays:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.relays = []
        process = self.process
        self.process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), STOP_ESCALATE_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()
