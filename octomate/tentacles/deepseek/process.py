"""One `dsh web` child: spawn it, confirm its URL from the banner, kill it with us.

The server is bound to loopback and owned by this process — the bind host is
fixed at `127.0.0.1` and deliberately not configurable. Authenticated harnesses
log their launch URL once at INFO. The returned base URL and subsequent
diagnostics omit the token.
Extension configuration lives in a fresh private home. Settings, credentials,
sessions and attachments use explicit paths under the native DSH home. DSH's
session write leases prevent concurrent writers to a shared session.

The CLI version check is advisory. Required Remote operations determine
compatibility because development checkouts can change their protocol without
changing the package version.
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
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit, urlunsplit

import anyio
from anyio.abc import ByteStream
from anyio.streams.buffered import BufferedByteReceiveStream
from pydantic import HttpUrl, SecretStr

from octomate.habitat.sbx import SbxHabitat
from octomate.tentacles.deepseek.schema import DrivenConfigPatch

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
TESTED_DSH_VERSION = "0.2.3-alpha.2"
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
    """The owned `dsh web` child, from spawn through readiness to shutdown."""

    executable: str
    port: int
    extra_args: list[str]
    dsh_home: Path
    ready_timeout: float
    browser_url: HttpUrl | None = None
    habitat: SbxHabitat | None = None
    env: dict[str, str] = field(default_factory=dict)
    guest_task: asyncio.Task[None] | None = field(default=None, init=False)
    guest_stop: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    process: asyncio.subprocess.Process | None = field(default=None, init=False)
    relays: list[asyncio.Task[None]] = field(default_factory=list, init=False)
    unknown_option: str | None = field(default=None, init=False)
    launch_token: SecretStr | None = field(default=None, init=False, repr=False)
    # Fresh extension configuration; durable settings and sessions use dsh_home.
    runtime_home: TemporaryDirectory[str] | None = field(default=None, init=False)

    diagnostics: deque[str] = field(
        default_factory=lambda: deque(maxlen=DIAGNOSTIC_LINES), init=False, repr=False
    )
    diagnostic_head: list[str] = field(default_factory=list, init=False, repr=False)
    diagnostic_count: int = field(default=0, init=False)
    ready: bool = field(default=False, init=False)
    stderr_reported: bool = field(default=False, init=False)

    def child_environment(self) -> dict[str, str]:
        """Keep provider/tool configuration, without exporting Octomate server settings."""
        return {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("OCTOMATE_")
        }

    async def check_version(self) -> None:
        probe = await asyncio.create_subprocess_exec(
            self.executable,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=self.child_environment(),
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
                "Could not determine dsh version; tested with %s. Continuing startup.",
                TESTED_DSH_VERSION,
            )
        elif version != TESTED_DSH_VERSION:
            logger.warning(
                "dsh version %s differs from Octomate's tested version %s; continuing startup",
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
        if self.habitat is not None:
            endpoint: asyncio.Future[HttpUrl] = (
                asyncio.get_running_loop().create_future()
            )
            self.guest_stop.clear()
            self.guest_task = asyncio.create_task(
                self.run_guest(self.habitat, endpoint)
            )
            try:
                return await asyncio.wait_for(
                    asyncio.shield(endpoint), self.ready_timeout
                )
            except BaseException:
                endpoint.cancel()
                self.guest_task.cancel()
                await asyncio.gather(self.guest_task, return_exceptions=True)
                raise
        try:
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
        except BaseException:
            await self.stop()
            raise

    async def run_guest(
        self, habitat: SbxHabitat, endpoint: asyncio.Future[HttpUrl]
    ) -> None:
        """Keep the guest process and its port mapping in one owning task."""
        try:
            launcher = await asyncio.to_thread(
                Path(__file__).with_name("guest.py").read_text
            )
            async with habitat.execute(
                "python",
                "-c",
                launcher,
                self.executable,
                "web",
                *self.extra_args,
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                NO_OPEN,
                *(
                    ["--trusted-host", urlsplit(str(self.browser_url)).netloc]
                    if self.browser_url is not None
                    else []
                ),
                workdir=str(self.dsh_home.parent),
                env=self.env,
                stderr=self.capture_diagnostic,
            ) as stream:
                stdout = BufferedByteReceiveStream(stream)
                try:
                    await self.connect_guest(habitat, stream, stdout, endpoint)
                finally:
                    await self.finish_guest(
                        stream, asyncio.create_task(self.drain_guest(stdout))
                    )
        except BaseException as error:
            self.log_startup_output()
            if isinstance(error, anyio.EndOfStream | anyio.IncompleteRead):
                error = RuntimeError(
                    "dsh web exited before reporting its guest URL; check startup output"
                )
            if not endpoint.done():
                endpoint.set_exception(error)
            else:
                raise

    async def connect_guest(
        self,
        habitat: SbxHabitat,
        stream: ByteStream,
        stdout: BufferedByteReceiveStream,
        endpoint: asyncio.Future[HttpUrl],
    ) -> None:
        """Read readiness before publishing the authenticated HTTP/WebSocket endpoint."""
        await stream.send(
            self.driven_patch(self.dsh_home).model_dump_json().encode() + b"\n"
        )
        while True:
            line = (await stdout.receive_until(b"\n", 65536)).decode(errors="replace")
            match = BANNER.search(line)
            if match is not None:
                break
            self.capture_diagnostic(line)
        launch_url = HttpUrl(match.group(1))
        token = dict(launch_url.query_params()).get("token")
        self.launch_token = SecretStr(token) if token else None
        if launch_url.host != "127.0.0.1" or launch_url.port is None:
            raise RuntimeError("DSH did not report its guest loopback listener")
        async with habitat.publish(launch_url.port) as address:
            endpoint.set_result(HttpUrl(address))
            self.ready = True
            relay = asyncio.create_task(self.drain_guest(stdout))
            try:
                await self.guest_stop.wait()
            finally:
                relay.cancel()
                await asyncio.gather(relay, return_exceptions=True)

    async def finish_guest(self, stream: ByteStream, relay: asyncio.Task[None]) -> None:
        """Signal EOF and let the guest supervisor reap DSH before detaching."""
        with anyio.CancelScope(shield=True):
            try:
                # An exited supervisor has already closed its end of the socket.
                with contextlib.suppress(OSError, anyio.BrokenResourceError):
                    await stream.send_eof()
                await asyncio.wait_for(relay, 5)
            finally:
                relay.cancel()
                await asyncio.gather(relay, return_exceptions=True)

    async def drain_guest(self, stdout: BufferedByteReceiveStream) -> None:
        try:
            while True:
                self.capture_diagnostic(
                    (await stdout.receive_until(b"\n", 65536)).decode(errors="replace")
                )
        except (anyio.EndOfStream, anyio.IncompleteRead):
            return

    async def launch(self, *, suppress_browser: bool) -> HttpUrl:
        """One spawn attempt, up to the banner.

        A private home excludes native profile and home-level plugin patches.
        Explicit paths share only settings, credentials, sessions and attachments.
        A dsh that exits or stays silent past `ready_timeout`
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
        self.runtime_home = await asyncio.to_thread(
            TemporaryDirectory, prefix="octomate-dsh-"
        )
        shared_home = await asyncio.to_thread(self.dsh_home.resolve)
        patch = Path(self.runtime_home.name) / "driven.json"
        await asyncio.to_thread(
            patch.write_text,
            self.driven_patch(shared_home).model_dump_json(),
        )
        process = await asyncio.create_subprocess_exec(
            self.executable,
            "web",
            *self.extra_args,
            "--patch",
            str(patch),
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
            env={**self.child_environment(), "DSH_HOME": self.runtime_home.name},
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
            self.log_startup_output()
            raise RuntimeError(
                f"dsh web did not report its URL within {self.ready_timeout:g}s"
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
        return base_url

    def driven_patch(self, shared_home: Path) -> DrivenConfigPatch:
        """Isolate extensions while keeping durable data in the runtime home."""
        return DrivenConfigPatch(
            [
                {
                    "id": "settings",
                    "config": {"path": shared_home / "settings.yaml"},
                },
                {
                    "id": "credentials",
                    "config": {"path": shared_home / ".credentials.yaml"},
                },
                {
                    "id": "session-persistence-jsonl",
                    "config": {"root": shared_home / "sessions"},
                },
                {"id": "attachment-local", "config": {"dshHome": shared_home}},
                {
                    "id": "sandbox",
                    "name": "@deepseek-ai/dsh-sandbox-local",
                    "disabled": False,
                    "config": {
                        "runnerCommand": [],
                        "runnerFailureSignatures": [],
                    },
                },
                {
                    "id": "sandbox-policy",
                    "name": "@deepseek-ai/dsh-sandbox-policy",
                    "disabled": False,
                    "config": {"mode": "workspace-write"},
                },
                {
                    "id": "bash-sandbox",
                    "name": "@deepseek-ai/dsh-bash-sandbox",
                    "disabled": os.name == "nt",
                },
                {
                    "id": "pwsh-sandbox",
                    "name": "@deepseek-ai/dsh-pwsh-sandbox",
                    "disabled": os.name != "nt",
                },
                {
                    "id": "fs-sandbox",
                    "name": "@deepseek-ai/dsh-fs-sandbox",
                    "disabled": False,
                },
                {
                    "id": "approval",
                    "name": "@deepseek-ai/dsh-user-approval",
                    "disabled": False,
                    "config": {"policy": "ask"},
                },
                {
                    "id": "permission",
                    "name": "@deepseek-ai/dsh-permission-presets",
                    "disabled": False,
                    "config": {
                        "presets": {
                            "read-only": {
                                "sandbox": "read-only",
                                "approval": "ask",
                            },
                            "workspace-write": {
                                "sandbox": "workspace-write",
                                "approval": "ask",
                            },
                            "danger-full-access": {
                                "sandbox": "danger-full-access",
                                "approval": "never",
                            },
                        },
                    },
                },
                {"id": "hmr", "disabled": True},
                {"id": "plugin-manager", "disabled": True},
                {"id": "tool-plugin-manager", "disabled": True},
                # Presets must not expose profile-management tools whose
                # host service is disabled in this managed process.
                {
                    "id": "agent-preset-registry",
                    "isolate": {"profileContext": True},
                },
            ]
        )

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
                logger.info("dsh web: %s", launch_url)
                return HttpUrl(urlunsplit(parts._replace(query="", fragment="")))
            if line:
                self.capture_diagnostic(line)
        code = await process.wait()
        # A refusal lands on stderr, whose relay can still be mid-line when
        # stdout closes: let it finish before deciding why the child died.
        await asyncio.gather(*self.relays)
        if self.unknown_option is not None:
            raise HarnessOptionUnsupportedError(self.unknown_option)
        self.log_startup_output()
        raise RuntimeError(f"dsh web exited before reporting a URL (code {code})")

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

    def log_startup_output(self) -> None:
        """What a dsh printed before failing to serve is the diagnosis, so it
        goes out as a log record of its own under the tentacle's tag — not
        folded into the exception, where it would render as part of a Python
        traceback it has nothing to do with."""
        if not self.diagnostic_count:
            return
        omitted = (
            self.diagnostic_count - len(self.diagnostic_head) - len(self.diagnostics)
        )
        lines = [
            *self.diagnostic_head,
            *(
                [f"({omitted} earlier lines omitted; full output at DEBUG)"]
                if omitted
                else []
            ),
            *self.diagnostics,
        ]
        logger.error("dsh web output before its start failed:\n%s", "\n".join(lines))

    async def stop(self) -> None:
        """SIGTERM, escalating to SIGKILL — a dsh that ignores the term still
        must not outlive octomate. A SIGKILLed octomate does orphan the child
        (dsh has no parent-liveness flag); that residual risk is documented,
        not solved here."""
        if self.guest_task is not None:
            self.guest_stop.set()
            await self.guest_task
            self.guest_task = None
            self.launch_token = None
        for task in self.relays:
            task.cancel()
        for task in self.relays:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.relays = []
        self.launch_token = None
        process = self.process
        self.process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), STOP_ESCALATE_SECONDS)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self.runtime_home is not None:
            await asyncio.to_thread(self.runtime_home.cleanup)
            self.runtime_home = None
