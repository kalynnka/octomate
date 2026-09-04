"""One `dsh web` child: spawn it, confirm its URL from the banner, kill it with us.

The server it starts has no TLS and no auth, which is only acceptable because
it is bound to loopback and owned by this process — the bind host is fixed at
`127.0.0.1` and deliberately not configurable. The port is the configured one,
fixed rather than ephemeral, and that is load-bearing: it is the address the
tentacle probes before starting anything, so a fixed port is what lets the
next octomate — or any other dsh client — attach to this harness instead of
starting a second writer of the same `$DSH_HOME`, which dsh does not lock and
which corrupts session logs.

`--no-open` is negotiated by retry rather than by a version probe. `dsh web`
parses its options without `allowUnknownOption`, so a dsh predating the flag
does not ignore it — it prints one refusal line and exits — and passing it
blindly would trade a browser tab for a harness that will not start at all.
Probing `dsh --version` first would instead cost every spawn an extra process,
to learn a number a checkout sitting between two releases reports wrongly
anyway.
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

# commander's refusal, printed to stderr by a dsh whose `web` command does not
# know an option, just before it exits without serving.
UNKNOWN_OPTION = re.compile(r"unknown option '([^']+)'")

# `dsh web` opens a browser tab on every start. For a harness octomate spawned,
# that is a window nobody asked for on a machine nobody may be sitting at.
NO_OPEN = "--no-open"

STOP_ESCALATE_SECONDS = 5.0


class HarnessOptionUnsupportedError(RuntimeError):
    """`dsh web` refused an option and exited without serving.

    `option` is the flag it named, which is the whole point of the type: it
    separates our own `--no-open` — droppable, since dropping it costs only a
    browser tab — from a flag the operator put in `extra_args`, which is theirs
    to fix and not ours to silently discard.
    """

    option: str

    def __init__(self, option: str) -> None:
        super().__init__(f"dsh web does not know the option {option}")
        self.option = option


@dataclass
class DeepseekProcess:
    executable: str
    port: int
    extra_args: list[str]
    dsh_home: Path
    ready_timeout: float
    process: asyncio.subprocess.Process | None = field(default=None, init=False)
    relays: list[asyncio.Task[None]] = field(default_factory=list, init=False)
    unknown_option: str | None = field(default=None, init=False)

    async def start(self) -> HttpUrl:
        """Spawn `dsh web` and return its base URL once the banner lands.

        `--no-open` is offered first and dropped only when this dsh refuses it,
        which costs one failed spawn against an old dsh and nothing against a
        current one. A refusal naming any other flag propagates: it came from
        `extra_args`, starting again without *our* flag would not fix it, and
        the retry would only hide the real error behind a second identical
        failure.
        """
        try:
            return await self.launch(suppress_browser=True)
        except HarnessOptionUnsupportedError as error:
            if error.option != NO_OPEN:
                raise
        logger.warning(
            "this dsh does not know %s; starting it again without it, so a browser tab will be opened",
            NO_OPEN,
        )
        return await self.launch(suppress_browser=False)

    async def launch(self, *, suppress_browser: bool) -> HttpUrl:
        """One spawn attempt, up to the banner.

        DSH_HOME is always the config's `dsh_home` — the config collects it,
        from the environment or dsh's own default, so nothing is decided or
        overridden here. A dsh that exits or stays silent past `ready_timeout`
        fails the attempt rather than being retried: a broken install is a
        broken install, and the one retry `start` does make is for a refused
        option, not for a dsh that cannot run.
        """
        self.unknown_option = None
        process = await asyncio.create_subprocess_exec(
            self.executable,
            "web",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            *([NO_OPEN] if suppress_browser else []),
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
        self.relays = [asyncio.create_task(self.watch_stderr(stderr))]
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
        self.relays.append(asyncio.create_task(self.relay_stdout(stdout)))
        logger.info("dsh ready at %s", base_url)
        return base_url

    async def read_banner(
        self, process: asyncio.subprocess.Process, stdout: asyncio.StreamReader
    ) -> HttpUrl:
        while line_bytes := await stdout.readline():
            line = line_bytes.decode(errors="replace").rstrip()
            if line:
                logger.info("dsh: %s", line)
            match = BANNER.search(line)
            if match is not None:
                return HttpUrl(match.group(1))
        code = await process.wait()
        # A refusal lands on stderr, whose relay can still be mid-line when
        # stdout closes: let it finish before deciding why the child died.
        await asyncio.gather(*self.relays)
        if self.unknown_option is not None:
            raise HarnessOptionUnsupportedError(self.unknown_option)
        raise RuntimeError(f"dsh web exited before reporting a URL (code {code})")

    async def watch_stderr(self, stderr: asyncio.StreamReader) -> None:
        """Relay stderr, keeping the flag named by commander's refusal line —
        the one signal separating a dsh too old for an option from a dsh that
        is simply broken."""
        while line_bytes := await stderr.readline():
            line = line_bytes.decode(errors="replace").rstrip()
            if not line:
                continue
            logger.warning("dsh: %s", line)
            match = UNKNOWN_OPTION.search(line)
            if match is not None:
                self.unknown_option = match.group(1)

    @staticmethod
    async def relay_stdout(stdout: asyncio.StreamReader) -> None:
        while line_bytes := await stdout.readline():
            line = line_bytes.decode(errors="replace").rstrip()
            if line:
                logger.info("dsh: %s", line)

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
