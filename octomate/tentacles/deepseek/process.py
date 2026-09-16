"""One `dsh web` child: spawn it, confirm its URL from the banner, kill it with us.

The server is bound to loopback and owned by this process — the bind host is
fixed at `127.0.0.1` and deliberately not configurable. Authenticated harnesses
print their launch URL once to the console. The returned base URL and subsequent
diagnostics omit the token.
The port is the configured one,
fixed rather than ephemeral, and that is load-bearing: it is the address the
tentacle probes before starting anything, so a fixed port is what lets the
next octomate — or any other dsh client — attach to this harness instead of
starting a second writer of the same `$DSH_HOME`, which dsh does not lock and
which corrupts session logs.

Compatibility is checked against the tested CLI release before starting; the
Remote API handshake remains authoritative because development checkouts can
change their protocol without changing the package version.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import HttpUrl, SecretStr

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
TESTED_DSH_VERSION = "0.1.6-alpha.1"
DIAGNOSTIC_LINES = 24
DIAGNOSTIC_WIDTH = 500
TOKEN_QUERY = re.compile(r"([?&]token=)[^\s&#]+")


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
    browser_url: HttpUrl | None = None
    process: asyncio.subprocess.Process | None = field(default=None, init=False)
    relays: list[asyncio.Task[None]] = field(default_factory=list, init=False)
    unknown_option: str | None = field(default=None, init=False)
    launch_token: SecretStr | None = field(default=None, init=False, repr=False)

    diagnostics: deque[str] = field(
        default_factory=lambda: deque(maxlen=DIAGNOSTIC_LINES), init=False, repr=False
    )
    diagnostic_head: list[str] = field(default_factory=list, init=False, repr=False)
    diagnostic_count: int = field(default=0, init=False)
    ready: bool = field(default=False, init=False)
    stderr_reported: bool = field(default=False, init=False)

    async def check_version(self) -> None:
        probe = await asyncio.create_subprocess_exec(
            self.executable,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(probe.communicate(), 10)
        except BaseException:
            if probe.returncode is None:
                probe.kill()
            await probe.wait()
            raise
        version = output.decode(errors="replace").strip()
        if (
            probe.returncode != 0
            or re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", version) is None
        ):
            logger.warning(
                "Could not determine dsh version; tested with %s. Checking Remote API compatibility at startup.",
                TESTED_DSH_VERSION,
            )
        elif version != TESTED_DSH_VERSION:
            logger.warning(
                "dsh version %s differs from Octomate's tested version %s; checking Remote API compatibility. Update dsh and Octomate together if it fails.",
                version,
                TESTED_DSH_VERSION,
            )
        else:
            logger.info(
                "dsh version %s matches Octomate's tested release; checking Remote API compatibility",
                version,
            )

    async def start(self) -> HttpUrl:
        """Spawn `dsh web` and return its base URL once the banner lands.

        `--no-open` is offered first and dropped only when this dsh refuses it,
        which costs one failed spawn against an old dsh and nothing against a
        current one. A refusal naming any other flag propagates: it came from
        `extra_args`, starting again without *our* flag would not fix it, and
        the retry would only hide the real error behind a second identical
        failure.
        """
        await self.check_version()
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
        self.diagnostics.clear()
        self.diagnostic_head.clear()
        self.diagnostic_count = 0
        self.ready = False
        self.stderr_reported = False
        self.unknown_option = None
        self.launch_token = None
        process = await asyncio.create_subprocess_exec(
            self.executable,
            "web",
            *self.extra_args,
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            *(
                ["--trusted-host", urlsplit(str(self.browser_url)).netloc]
                if self.browser_url is not None
                else []
            ),
            *([NO_OPEN] if suppress_browser else []),
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
                f"dsh web did not report its URL within {self.ready_timeout:g}s{self.startup_diagnostics()}"
            ) from None
        except BaseException:
            await self.stop()
            raise
        self.ready = True
        if self.diagnostic_count:
            logger.warning(
                "dsh startup produced %d diagnostic lines (details at DEBUG)",
                self.diagnostic_count,
            )
        self.relays.append(asyncio.create_task(self.relay_stdout(stdout)))
        logger.info("dsh ready at %s", base_url)
        return base_url

    async def read_banner(
        self, process: asyncio.subprocess.Process, stdout: asyncio.StreamReader
    ) -> HttpUrl:
        while line_bytes := await stdout.readline():
            line = line_bytes.decode(errors="replace").rstrip()
            match = BANNER.search(line)
            if match is not None:
                launch_url = HttpUrl(match.group(1))
                token = dict(launch_url.query_params()).get("token")
                self.launch_token = SecretStr(token) if token else None
                parts = urlsplit(str(launch_url))
                if self.browser_url is not None:
                    browser_parts = urlsplit(str(self.browser_url))
                    launch_url = HttpUrl(
                        urlunsplit(browser_parts._replace(query=parts.query))
                    )
                # Write directly to stdout so logging integrations do not receive the token.
                print(f"dsh web: {launch_url}", flush=True)
                return HttpUrl(urlunsplit(parts._replace(query="", fragment="")))
            if line:
                self.capture_diagnostic(line)
        code = await process.wait()
        # A refusal lands on stderr, whose relay can still be mid-line when
        # stdout closes: let it finish before deciding why the child died.
        await asyncio.gather(*self.relays)
        if self.unknown_option is not None:
            raise HarnessOptionUnsupportedError(self.unknown_option)
        raise RuntimeError(
            f"dsh web exited before reporting a URL (code {code}){self.startup_diagnostics()}"
        )

    async def watch_stderr(self, stderr: asyncio.StreamReader) -> None:
        """Relay stderr, keeping the flag named by commander's refusal line —
        the one signal separating a dsh too old for an option from a dsh that
        is simply broken."""
        while line_bytes := await stderr.readline():
            line = line_bytes.decode(errors="replace").rstrip()
            if not line:
                continue
            self.capture_diagnostic(line)
            if self.ready and not self.stderr_reported:
                self.stderr_reported = True
                logger.warning(
                    "dsh stderr: %s (further output at DEBUG)",
                    TOKEN_QUERY.sub(r"\1<redacted>", line)[:DIAGNOSTIC_WIDTH],
                )
            match = UNKNOWN_OPTION.search(line)
            if match is not None:
                self.unknown_option = match.group(1)

    async def relay_stdout(self, stdout: asyncio.StreamReader) -> None:
        while line_bytes := await stdout.readline():
            line = line_bytes.decode(errors="replace").rstrip()
            if line:
                self.capture_diagnostic(line)

    def capture_diagnostic(self, line: str) -> None:
        line = TOKEN_QUERY.sub(r"\1<redacted>", line)
        logger.debug("dsh: %s", line)
        if len(self.diagnostic_head) < 4:
            self.diagnostic_head.append(line[:DIAGNOSTIC_WIDTH])
        else:
            self.diagnostics.append(line[:DIAGNOSTIC_WIDTH])
        self.diagnostic_count += 1

    def startup_diagnostics(self) -> str:
        if not self.diagnostic_count:
            return ""
        omitted = (
            self.diagnostic_count - len(self.diagnostic_head) - len(self.diagnostics)
        )
        prefix = (
            f"\n({omitted} earlier lines omitted; full output at DEBUG)"
            if omitted
            else ""
        )
        return (
            "\n"
            + "\n".join(self.diagnostic_head)
            + prefix
            + "\n"
            + "\n".join(self.diagnostics)
        )

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
        self.launch_token = None
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
