"""SRT-confined host processes with persistent owner storage."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shlex
import shutil
import signal
import socket
import sys
from collections import deque
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import ClassVar

import anyio
from anyio import to_thread
from anyio.abc import SocketStream
from pydantic import UUID7, BaseModel

from octomate.config.habitat import SrtHabitatConfig
from octomate.habitat.base import HabitatError, WorkspaceHabitat
from octomate.habitat.executor import CommandEvent, Request
from octomate.schemas.volumes import Volume

logger = logging.getLogger(__name__)


@dataclass
class SrtHabitat(WorkspaceHabitat):
    os_sandbox: ClassVar[bool] = False
    config: SrtHabitatConfig
    user_id: UUID7
    root: Path  # Owner storage; the shared trust scope for this habitat's processes.
    name: str = "default"  # Configured habitat name, independent of the owner.
    # Read-only workspace; runtime and harness state stay writable.
    read_only: bool = False
    # Disposable runtime storage for workspace-scoped runs.
    scratch: Path | None = None
    # Writable harness state outside the workspace.
    state_paths: tuple[Path, ...] = ()
    settings: TemporaryDirectory[str] | None = field(default=None, init=False)
    executable: str | None = field(default=None, init=False)
    node: str | None = field(default=None, init=False)
    launcher: str = field(default="", init=False)
    processes: set[asyncio.subprocess.Process] = field(default_factory=set, init=False)

    @asynccontextmanager
    async def scope(
        self,
        cwd: Path,
        *,
        read_only: bool = False,
        read_paths: tuple[Path, ...] = (),
        state_paths: tuple[Path, ...] = (),
    ) -> AsyncGenerator[WorkspaceHabitat]:
        root = Path(await anyio.Path(cwd).resolve())
        states = tuple([Path(await anyio.Path(path).resolve()) for path in state_paths])
        if any(not path.is_relative_to(self.root) for path in (root, *states)):
            raise HabitatError(
                "Workspace and session storage must belong to the habitat owner"
            )
        with TemporaryDirectory(prefix="octo-", dir="/tmp") as scratch:
            habitat = SrtHabitat(
                config=self.config.model_copy(
                    update={"read_paths": [*self.config.read_paths, *read_paths]}
                ),
                user_id=self.user_id,
                root=root,
                name=self.name,
                read_only=read_only,
                scratch=Path(scratch),
                state_paths=states,
            )
            try:
                await habitat.prepare()
                yield habitat
            finally:
                with anyio.CancelScope(shield=True):
                    await habitat.stop()

    def command_prefix(self) -> list[str]:
        if self.settings is None or self.executable is None or self.node is None:
            raise HabitatError(
                "Prepare the workspace habitat before launching commands"
            )
        return [
            self.node,
            str(Path(__file__).with_name("srt-workspace.mjs")),
            json.dumps(
                {
                    "runtime": str(Path(self.executable).with_name("index.js")),
                    "settings": str(Path(self.settings.name) / "settings.json"),
                    "root": str(self.root),
                }
            ),
        ]

    @property
    def id(self) -> str:
        return f"srt-{self.name}-{self.user_id.hex}"

    @property
    def idle_timeout(self) -> float:
        return self.config.idle_timeout

    def path(self, volume: Volume) -> PurePosixPath:
        path = volume.path.resolve()
        if volume.owner_id != self.user_id or not path.is_relative_to(
            self.root.resolve()
        ):
            raise HabitatError("Only the owner's volumes are visible in their habitat")
        return PurePosixPath(path)

    def environment(self) -> dict[str, str]:
        state = self.scratch or self.root
        return {
            "PATH": os.pathsep.join(
                (
                    str(Path(sys.prefix) / "bin"),
                    str(Path(sys.base_prefix) / "bin"),
                    "/usr/bin",
                    "/bin",
                    "/usr/sbin",
                    "/sbin",
                )
            ),
            "HOME": str(state / "config" / "home"),
            "TMPDIR": str(state / "cache" / "tmp"),
            "PYTHONDONTWRITEBYTECODE": "1",
            # Local core/provider endpoints must use the proxy too; direct
            # loopback connections are denied by this habitat's policy.
            "NO_PROXY": "",
            "no_proxy": "",
        }

    async def request[T: BaseModel](
        self,
        request: Request,
        expected: type[T],
        *,
        workdir: str,
        on_event: Callable[[CommandEvent], None] | None = None,
    ) -> T:
        cwd = Path(await anyio.Path(workdir).resolve())
        if not cwd.is_relative_to(self.root):
            raise HabitatError(
                "SRT working directory must be inside the owner's storage"
            )
        return await super().request(
            request, expected, workdir=str(cwd), on_event=on_event
        )

    async def prepare(self) -> None:
        if platform.system() != "Darwin":
            raise HabitatError(
                "SRT habitat execution is currently qualified for macOS only"
            )
        if self.settings is not None:
            raise HabitatError("SRT habitat is already prepared")
        executable = shutil.which(self.config.executable)
        if executable is None:
            raise HabitatError("Install SRT and configure its executable")
        await to_thread.run_sync(self.configure, executable)

    def configure(self, executable: str) -> None:
        """Build a core-owned policy while keeping trusted runtimes outside its write scope."""
        self.root = self.root.resolve()
        node = shutil.which("node")
        if node is None:
            raise HabitatError("Install Node.js for the SRT SDK")
        runtimes = [Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()]
        if any(
            path.is_relative_to(self.root)
            for path in [Path(executable).resolve(), Path(node).resolve(), *runtimes]
        ):
            raise HabitatError(
                "Install trusted SRT, Node.js and Python runtimes outside owner-writable storage"
            )
        self.executable = str(Path(executable).resolve())
        self.node = str(Path(node).resolve())
        if not Path(self.executable).with_name("index.js").is_file():
            raise HabitatError(
                "Configure the srt executable from the @anthropic-ai/sandbox-runtime npm package"
            )
        self.launcher = Path(__file__).with_name("srt.mjs").read_text()
        for key in ("HOME", "TMPDIR"):
            Path(self.environment()[key]).mkdir(parents=True, exist_ok=True)
        self.settings = TemporaryDirectory(prefix="octomate-srt-")
        if Path(self.settings.name).resolve().is_relative_to(self.root):
            self.settings.cleanup()
            self.settings = None
            raise HabitatError(
                "SRT policy must be stored outside owner-writable storage"
            )
        # Literal paths avoid interpreting punctuation in an owner's path as a glob.
        readable = [
            Path(path)
            for path in (
                "/System",
                "/usr",
                "/bin",
                "/sbin",
                "/dev",
                # Permit reading this symlink; its /private/etc target stays denied.
                "/etc",
                "/private/var/select",
                "/private/etc/localtime",
                "/private/etc/hosts",
            )
        ]
        readable.extend(
            [self.root, *runtimes, *(p.resolve() for p in self.config.read_paths)]
        )
        if self.scratch is not None:
            readable.append(self.scratch)
        readable.extend(path.resolve() for path in self.state_paths)
        writable = [] if self.read_only else [str(self.root)]
        writable.extend(str(path.resolve()) for path in self.state_paths)
        if self.scratch is not None or self.read_only:
            writable.extend(self.environment()[key] for key in ("HOME", "TMPDIR"))
        policy = {
            "network": {
                "allowedDomains": [],
                "deniedDomains": self.config.denied_domains,
                "strictAllowlist": False,
                "allowLocalBinding": False,
            },
            "filesystem": {
                "denyRead": ["/"],
                "allowRead": [
                    {"path": str(path), "literal": True} for path in readable
                ],
                "allowWrite": [{"path": path, "literal": True} for path in writable],
                # SRT adds this shared temp directory even with an allowlist.
                "denyWrite": ["/tmp/claude", "/private/tmp/claude"],
            },
        }
        (Path(self.settings.name) / "settings.json").write_text(json.dumps(policy))

    @asynccontextmanager
    async def resident(
        self,
        *command: str,
        workdir: str,
        env: dict[str, str] | None = None,
        stderr: Callable[[str], None] | None = None,
    ) -> AsyncGenerator[SocketStream]:
        if self.settings is None or self.executable is None or self.node is None:
            raise HabitatError("Prepare the SRT habitat before starting a process")
        if not command:
            raise ValueError("Command must not be empty")
        cwd = Path(await anyio.Path(workdir).resolve())
        if not cwd.is_relative_to(self.root):
            raise HabitatError(
                "SRT working directory must be inside the owner's storage"
            )
        parent, child = socket.socketpair()
        with parent, child:
            async with await SocketStream.from_socket(parent) as stream:
                process = await asyncio.create_subprocess_exec(
                    self.node,
                    "--input-type=module",
                    "-e",
                    self.launcher,
                    Path(self.executable).with_name("index.js").as_uri(),
                    str(Path(self.settings.name) / "settings.json"),
                    shlex.join(
                        [
                            "/usr/bin/env",
                            *(
                                f"{key}={value}"
                                for key, value in (
                                    self.environment() | (env or {})
                                ).items()
                            ),
                            *command,
                        ]
                    ),
                    cwd=cwd,
                    env=self.environment()
                    | {"PATH": os.environ.get("PATH", os.defpath)},
                    stdin=child.fileno(),
                    stdout=child.fileno(),
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                child.close()
                self.processes.add(process)
                if process.stderr is None:
                    raise RuntimeError("SRT stderr pipe was not opened")
                reader = asyncio.create_task(self.read_stderr(process.stderr, stderr))
                failed = False
                try:
                    yield stream
                except BaseException:
                    failed = True
                    raise
                finally:
                    await self.finish(stream, process, reader, failed=failed)

    async def finish(
        self,
        stream: SocketStream,
        process: asyncio.subprocess.Process,
        reader: asyncio.Task[bytes],
        *,
        failed: bool,
    ) -> None:
        """Close stdio, reap the resident, and preserve the caller's primary error."""
        with anyio.CancelScope(shield=True):
            await stream.aclose()
            if not failed:
                with anyio.move_on_after(2):
                    await process.wait()
            await self.reap(process)
            errors = await reader
        if process.returncode and (errors or not failed):
            logger.error(
                "SRT process failed (exit %s): %s",
                process.returncode,
                errors.decode("utf-8", "replace"),
            )
        if process.returncode and not failed:
            raise HabitatError(
                f"SRT process failed (exit {process.returncode}); check process stderr"
            )

    async def reap(self, process: asyncio.subprocess.Process) -> None:
        """Terminate the process group, including children left after the leader exits."""
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        with anyio.move_on_after(2):
            await process.wait()
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        self.processes.discard(process)

    async def read_stderr(
        self, source: asyncio.StreamReader, callback: Callable[[str], None] | None
    ) -> bytes:
        errors: deque[bytes] = deque(maxlen=16)
        pending = b""
        while chunk := await source.read(4096):
            errors.append(chunk)
            if callback is None:
                continue
            *lines, pending = (pending + chunk).split(b"\n")
            for line in lines:
                callback(line.decode("utf-8", "replace").rstrip("\r"))
            if len(pending) >= 4096:
                callback(pending.decode("utf-8", "replace"))
                pending = b""
        if pending and callback is not None:
            callback(pending.decode("utf-8", "replace"))
        return b"".join(errors)

    async def stop(self) -> None:
        if self.executor is not None:
            await self.executor.close()
        for process in tuple(self.processes):
            await self.reap(process)
        if self.settings is not None:
            self.settings.cleanup()
            self.settings = None
        self.executable = None
        self.node = None
        self.launcher = ""
