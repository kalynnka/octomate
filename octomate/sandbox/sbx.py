"""Local sbx lifecycle operations and scoped guest command streams."""

from __future__ import annotations

import asyncio
import os
import platform
import re
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypedDict, Unpack

import sh
from anyio.abc import UNIXSocketStream
from pydantic import UUID7

from octomate.config.sandbox import SbxSandboxConfig
from octomate.sandbox.base import Sandbox, SandboxError

COMMAND_TIMEOUT = 30.0
PREPARE_TIMEOUT = 300.0


class SbxKitArgs(TypedDict):
    """Inputs declared by the bundled sandbox kit's args block."""

    user_id: UUID7
    image: str
    cpus: int
    memory_mib: int
    internet_access: Literal["true", "false"]


class SbxOptions(TypedDict, total=False):
    """CLI controls; the kit defines the sandbox itself."""

    name: str
    pull: Literal["always", "missing", "never"]
    skills: Literal["off", "readonly", "readwrite"]
    no_progress: bool
    quiet: bool
    kit: list[str]
    kit_arg: list[str]


@dataclass
class SbxSandbox(Sandbox):
    config: SbxSandboxConfig
    user_id: UUID7
    managed: bool = field(default=False, init=False)  # Name found or create tried.

    @property
    def name(self) -> str:
        return f"octomate-{self.user_id.hex}"

    @property
    def idle_timeout(self) -> float:
        return self.config.idle_timeout

    def cli(self) -> sh.Command[sh.RunningCommand]:
        """Keep host credentials out of child processes and use ordinary stdio."""
        try:
            return sh.Command(self.config.executable).bake(
                "--debug=false",
                _env={
                    key: os.environ[key]
                    for key in ("PATH", "HOME", "TMPDIR", "XDG_RUNTIME_DIR")
                    if key in os.environ
                },
                _tty_in=False,
                _tty_out=False,
                _err=os.devnull,
                _async=True,
                _return_cmd=True,
            )
        except sh.CommandNotFound:
            raise SandboxError(
                "Cannot launch sbx; check the configured executable"
            ) from None

    async def command(
        self,
        *args: str,
        **options: Unpack[SbxOptions],
    ) -> bytes:
        process: sh.RunningCommand | None = None
        try:
            process = self.cli()(
                *args,
                **options,
                _timeout=PREPARE_TIMEOUT if args[0] == "create" else COMMAND_TIMEOUT,
            )
            await process
            return process.stdout
        except sh.TimeoutException:
            raise SandboxError(f"sbx {args[0]} timed out") from None
        except sh.ErrorReturnCode as error:
            raise SandboxError(
                f"sbx {args[0]} failed (exit {error.exit_code}); "
                "check Docker sign-in, daemon readiness and image availability"
            ) from None
        except (OSError, sh.ForkException):
            raise SandboxError(
                "Cannot launch sbx; check the configured executable"
            ) from None
        finally:
            if process is not None and process.is_alive():
                with suppress(ProcessLookupError):
                    process.kill()
                with suppress(sh.ErrorReturnCode, sh.TimeoutException):
                    await process

    async def preflight(self) -> None:
        system, machine = platform.system(), platform.machine()
        if system == "Darwin":
            if machine != "arm64" or int(platform.mac_ver()[0].split(".")[0]) < 14:
                raise SandboxError("Local sbx requires Apple Silicon and macOS 14+")
        elif system == "Linux":
            release = platform.freedesktop_os_release()
            if release.get("ID") != "ubuntu" or tuple(
                int(part) for part in release.get("VERSION_ID", "0").split(".")
            ) < (24, 4):
                raise SandboxError("Local sbx requires Ubuntu 24.04+")
            if machine not in {"x86_64", "aarch64"} or not os.access(
                "/dev/kvm", os.R_OK | os.W_OK
            ):
                raise SandboxError(
                    "Local sbx requires accessible KVM on amd64 or arm64"
                )
        else:
            raise SandboxError("Local sbx supports macOS and Linux hosts only")
        version = await self.command("version")
        if not re.search(
            rb"(?:Client Version:\s+|sbx version )v"
            + re.escape(self.config.version.encode())
            + rb"(?:\s|$)",
            version,
        ):
            raise SandboxError(f"Install the pinned sbx release {self.config.version}")
        forwarding = await self.command("settings", "get", "ssh.agentForwardingEnabled")
        if forwarding.strip() != b"false":
            raise SandboxError(
                "Turn off sbx SSH agent forwarding: sbx settings set "
                "ssh.agentForwardingEnabled false, then sbx daemon restart"
            )

    async def prepare(self) -> None:
        await self.preflight()
        names = (await self.command("ls", quiet=True)).decode().splitlines()
        kit_args: SbxKitArgs = {
            "user_id": self.user_id,
            "image": self.config.image,
            "cpus": self.config.cpus,
            "memory_mib": self.config.memory_mib,
            "internet_access": "true" if self.config.internet_access else "false",
        }
        self.managed = True
        if self.name not in names:
            kit = Path(__file__).with_suffix("") / "kits"
            await self.command(
                "create",
                str(kit),
                kit_arg=[f"{key}={value}" for key, value in kit_args.items()],
                kit=[] if self.config.internet_access else [str(kit / "offline")],
                name=self.name,
                pull="missing",
                skills="off",
                no_progress=True,
            )
        metadata = await self.command(
            "exec",
            self.name,
            "printenv",
            *(f"OCTOMATE_SANDBOX_{key.upper()}" for key in kit_args),
        )
        if metadata.splitlines() != [
            str(value).encode() for value in kit_args.values()
        ]:
            raise SandboxError(
                "Existing sandbox owner, image, resource or network settings differ; no automatic replacement is allowed"
            )

    @asynccontextmanager
    async def execute(
        self, *command: str, workdir: str, env: dict[str, str] | None = None
    ) -> AsyncGenerator[UNIXSocketStream]:
        """Connect guest stdio directly to a byte stream, bypassing sh buffering."""
        parent, child = socket.socketpair()
        stream: UNIXSocketStream | None = None
        process: sh.RunningCommand | None = None
        try:
            stream = await UNIXSocketStream.from_socket(parent)
            with child.makefile("rwb", buffering=0) as stdio:
                process = self.cli().bake(
                    "exec",
                    interactive=True,
                    workdir=workdir,
                    env=[f"{key}={value}" for key, value in (env or {}).items()],
                )(
                    self.name,
                    *command,
                    _in=stdio,
                    _out=stdio.fileno(),
                )
        except BaseException as error:
            if stream is not None:
                await stream.aclose()
            else:
                parent.close()
            if isinstance(error, (OSError, sh.ForkException)):
                raise SandboxError("Cannot launch the sandbox command") from None
            raise
        finally:
            child.close()
        try:
            async with stream:
                yield stream
        finally:
            with suppress(ProcessLookupError):
                if process.is_alive():
                    process.terminate()
            try:
                async with asyncio.timeout(5):
                    await process
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                with suppress(sh.ErrorReturnCode):
                    await process
            except sh.ErrorReturnCode:
                pass

    async def stop(self) -> None:
        if self.managed:
            names = (await self.command("ls", quiet=True)).decode().splitlines()
            if self.name in names:
                await self.command("stop", self.name)
