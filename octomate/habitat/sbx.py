"""Local sbx lifecycle operations and scoped guest command streams."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import socket
from collections import deque
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, TypedDict, Unpack

import anyio
from anyio.abc import SocketStream
from pydantic import UUID7, BaseModel, Field, TypeAdapter
from yarl import URL

from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.base import HabitatError, HostedHabitat
from octomate.schemas.volumes import Volume
from octomate.telemetry import habitat_logfire

COMMAND_TIMEOUT = 30.0
PREPARE_TIMEOUT = 300.0
logger = logging.getLogger(__name__)


class PublishedPort(BaseModel):
    host_ip: str
    host_port: int = Field(ge=1, le=65535)
    sandbox_port: int = Field(ge=1, le=65535)


published_ports_adapter = TypeAdapter(list[PublishedPort])


class SbxKitArgs(TypedDict):
    """Inputs declared by the bundled sandbox kit's args block."""

    user_id: UUID7
    root: Path
    config: Path
    image: str
    cpus: int
    memory_mib: int
    internet_access: Literal["true", "false"]
    denied_domains: str


class SbxOptions(TypedDict, total=False):
    """CLI controls; the kit defines the sandbox itself."""

    name: str
    pull: Literal["always", "missing", "never"]
    skills: Literal["off", "readonly", "readwrite"]
    no_progress: bool
    quiet: bool
    deny_network: list[str]
    kit_arg: list[str]


@dataclass
class SbxHabitat(HostedHabitat):
    config: SbxHabitatConfig
    user_id: UUID7
    root: Path  # The owner's storage on the host, mounted when the sandbox is made.
    config_dir: Path  # The owner's agent-configuration volume, beneath the root.
    name: str = "default"  # Configured habitat name, independent of the owner.
    managed: bool = field(default=False, init=False)  # Name found or create tried.

    @property
    def id(self) -> str:
        return f"octomate-{self.name}-{self.user_id.hex}"

    @property
    def idle_timeout(self) -> float:
        return self.config.idle_timeout

    @property
    def core_url(self) -> str:
        if not self.config.internet_access:
            raise HabitatError(
                "Core HTTP access is blocked by this sandbox's deny-all network policy"
            )
        address = URL(str(self.config.core_address)).origin()
        return str(address.with_host("host.docker.internal"))

    def path(self, volume: Volume) -> PurePosixPath:
        """Local sbx shows a mounted host path at the same path in the guest."""
        if not volume.path.is_relative_to(self.root):
            raise HabitatError(
                "Only the owner's own volumes are visible in their sandbox"
            )
        return PurePosixPath(volume.path)

    @asynccontextmanager
    async def publish(self, port: int) -> AsyncGenerator[str]:
        """Expose a guest TCP port on ephemeral host loopback for this context."""
        data = await self.control(
            "ports", self.id, "--publish", f"{port}/tcp4", "--json"
        )
        ports = published_ports_adapter.validate_json(data)
        matches = [
            binding
            for binding in ports
            if binding.sandbox_port == port and binding.host_ip == "127.0.0.1"
        ]
        if len(matches) != 1:
            raise HabitatError("sbx did not return one published port for the service")
        binding = matches[0]
        try:
            yield f"http://127.0.0.1:{binding.host_port}"
        finally:
            with anyio.CancelScope(shield=True):
                await self.control(
                    "ports",
                    self.id,
                    "--unpublish",
                    f"{port}/tcp4",
                )

    @asynccontextmanager
    async def cli(
        self,
        *args: str,
        stdin: int = asyncio.subprocess.DEVNULL,
        stdout: int = asyncio.subprocess.PIPE,
    ) -> AsyncGenerator[asyncio.subprocess.Process]:
        """Own a child process with ordinary stdio and no host credentials."""
        try:
            process = await asyncio.create_subprocess_exec(
                self.config.executable,
                "--debug=false",
                *args,
                env={
                    key: os.environ[key]
                    for key in (
                        "PATH",
                        "HOME",
                        "TMPDIR",
                        "XDG_RUNTIME_DIR",
                        "SBX_NO_TELEMETRY",
                    )
                    if key in os.environ
                },
                stdin=stdin,
                stdout=stdout,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            raise HabitatError(
                "Cannot launch sbx; check the configured executable"
            ) from None
        try:
            yield process
        finally:
            with anyio.CancelScope(shield=True):
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                await process.wait()

    async def control(
        self,
        *args: str,
        **options: Unpack[SbxOptions],
    ) -> bytes:
        arguments = list(args)
        for key, value in options.items():
            flag = "--" + key.replace("_", "-")
            if isinstance(value, bool):
                if value:
                    arguments.append(flag)
            elif isinstance(value, list):
                arguments.extend(f"{flag}={item}" for item in value)
            else:
                arguments.append(f"{flag}={value}")
        try:
            with habitat_logfire.span(
                "sbx {operation}", operation=args[0], habitat=self.id
            ):
                async with self.cli(*arguments) as process:
                    async with asyncio.timeout(
                        PREPARE_TIMEOUT if args[0] == "create" else COMMAND_TIMEOUT
                    ):
                        stdout, stderr = await process.communicate()
            if process.returncode:
                logger.error(
                    "sbx %s failed: %s", args[0], stderr.decode("utf-8", "replace")
                )
                raise HabitatError(
                    f"sbx {args[0]} failed (exit {process.returncode}); "
                    "check Docker sign-in, daemon readiness and image availability"
                )
            return stdout
        except TimeoutError:
            raise HabitatError(f"sbx {args[0]} timed out") from None

    async def preflight(self) -> None:
        system, machine = platform.system(), platform.machine()
        if system == "Darwin":
            if machine != "arm64" or int(platform.mac_ver()[0].split(".")[0]) < 14:
                raise HabitatError("Local sbx requires Apple Silicon and macOS 14+")
        elif system == "Linux":
            release = platform.freedesktop_os_release()
            if release.get("ID") != "ubuntu" or tuple(
                int(part) for part in release.get("VERSION_ID", "0").split(".")
            ) < (24, 4):
                raise HabitatError("Local sbx requires Ubuntu 24.04+")
            if machine not in {"x86_64", "aarch64"} or not os.access(
                "/dev/kvm", os.R_OK | os.W_OK
            ):
                raise HabitatError(
                    "Local sbx requires accessible KVM on amd64 or arm64"
                )
        else:
            raise HabitatError("Local sbx supports macOS and Linux hosts only")
        version = await self.control("version")
        if not re.search(
            rb"(?:Client Version:\s+|sbx version )v"
            + re.escape(self.config.version.encode())
            + rb"(?:\s|$)",
            version,
        ):
            raise HabitatError(f"Install the pinned sbx release {self.config.version}")
        forwarding = await self.control("settings", "get", "ssh.agentForwardingEnabled")
        if forwarding.strip() != b"false":
            raise HabitatError(
                "Turn off sbx SSH agent forwarding: sbx settings set "
                "ssh.agentForwardingEnabled false, then sbx daemon restart"
            )

    async def prepare(self) -> None:
        await self.preflight()
        names = (await self.control("ls", quiet=True)).decode().splitlines()
        kit_args: SbxKitArgs = {
            "user_id": self.user_id,
            "root": self.root,
            "config": self.config_dir,
            "image": self.config.image,
            "cpus": self.config.cpus,
            "memory_mib": self.config.memory_mib,
            "internet_access": "true" if self.config.internet_access else "false",
            "denied_domains": json.dumps(
                sorted(set(self.config.denied_domains)), separators=(",", ":")
            ),
        }
        self.managed = True
        if self.id not in names:
            # sbx takes host paths only when it makes the sandbox, so the whole
            # root is mounted now and every volume made under it later is seen.
            # The agents' credential file is written beneath the configuration
            # volume, where the harness is told to keep its configuration.
            await anyio.Path(self.config_dir).mkdir(parents=True, exist_ok=True)
            # sbx requires port-free IPv6 addresses in CIDR notation.
            denied_domains = [
                f"{domain[1:-1]}/128" if domain.endswith("]") else domain
                for domain in self.config.denied_domains
            ]
            await self.control(
                "create",
                str(Path(__file__).parent),
                str(self.root),
                kit_arg=[f"{key}={value}" for key, value in kit_args.items()],
                deny_network=denied_domains if self.config.internet_access else ["**"],
                name=self.id,
                pull="missing",
                skills="off",
                no_progress=True,
            )
            return
        metadata = await self.control(
            "exec",
            self.id,
            "printenv",
            *(f"OCTOMATE_HABITAT_{key.upper()}" for key in kit_args),
        )
        if metadata.splitlines() != [
            str(value).encode() for value in kit_args.values()
        ]:
            raise HabitatError(
                "Existing sandbox owner, storage, image, resource or network settings differ; no automatic replacement is allowed"
            )

    @asynccontextmanager
    async def resident(
        self,
        *command: str,
        workdir: str,
        env: dict[str, str] | None = None,
        stderr: Callable[[str], None] | None = None,
    ) -> AsyncGenerator[SocketStream]:
        """Keep a guest process attached to a byte stream until its owner exits."""
        parent, child = socket.socketpair()
        with parent, child:
            async with (
                await SocketStream.from_socket(parent) as stream,
                self.cli(
                    "exec",
                    "--interactive",
                    f"--workdir={workdir}",
                    *(f"--env={key}={value}" for key, value in (env or {}).items()),
                    self.id,
                    *command,
                    stdin=child.fileno(),
                    stdout=child.fileno(),
                ) as process,
            ):
                child.close()
                if process.stderr is None:
                    raise RuntimeError("Sandbox stderr pipe was not opened")
                reader = asyncio.create_task(self.read_stderr(process.stderr, stderr))
                failed = False
                try:
                    yield stream
                except BaseException:
                    failed = True
                    raise
                finally:
                    with anyio.CancelScope(shield=True):
                        await stream.aclose()
                        if failed and process.returncode is None:
                            with suppress(ProcessLookupError):
                                process.terminate()
                        with anyio.move_on_after(5) as deadline:
                            await process.wait()
                        with suppress(ProcessLookupError):
                            if process.returncode is None:
                                process.kill()
                        await process.wait()
                        errors = await reader
                    if process.returncode and (errors or not failed):
                        logger.error(
                            "Sandbox command failed (exit %s): %s",
                            process.returncode,
                            errors.decode("utf-8", "replace"),
                        )
                    if not failed and deadline.cancel_called:
                        raise HabitatError(
                            "Sandbox command did not exit after closing stdio"
                        )
                    if not failed and process.returncode:
                        raise HabitatError(
                            f"Sandbox command failed (exit {process.returncode}); "
                            "check the process stderr"
                        )

    async def read_stderr(
        self, source: asyncio.StreamReader, callback: Callable[[str], None] | None
    ) -> bytes:
        """Deliver live diagnostics and retain a bounded tail for exit failures."""
        errors: deque[bytes] = deque(maxlen=100)
        pending = b""
        while chunk := await source.read(65536):
            errors.append(chunk)
            if callback is None:
                continue
            *lines, pending = (pending + chunk).split(b"\n")
            for line in lines:
                callback(line.decode("utf-8", "replace").rstrip("\r"))
        if callback is not None and pending:
            callback(pending.decode("utf-8", "replace").rstrip("\r"))
        return b"".join(errors)

    async def stop(self) -> None:
        if self.executor is not None:
            await self.executor.close()
        if self.managed:
            names = (await self.control("ls", quiet=True)).decode().splitlines()
            if self.id in names:
                await self.control("stop", self.id)
