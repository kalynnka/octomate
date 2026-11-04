"""Local sbx subprocesses and the sandbox agent's stdio connection."""

from __future__ import annotations

import asyncio
import os
import platform
import re
import socket
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TypedDict, Unpack

import sh
from anyio.abc import UNIXSocketStream
from pydantic import UUID7

from octomate.config.sandbox import SbxSandboxConfig
from octomate.sandbox.base import SandboxError

COMMAND_TIMEOUT = 30.0
PREPARE_TIMEOUT = 300.0


class SbxOptions(TypedDict, total=False):
    name: str
    template: str
    pull: str
    cpus: int
    memory: str
    skills: str
    no_progress: bool
    env: list[str]
    quiet: bool


@dataclass
class SbxSandbox:
    config: SbxSandboxConfig
    user_id: UUID7
    managed: bool = field(default=False, init=False)  # Name found or create tried.
    process: sh.RunningCommand | None = field(default=None, init=False)

    @property
    def name(self) -> str:
        return f"octomate-{self.user_id.hex}"

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

    async def prepare(self) -> None:
        await self.preflight()
        names = (await self.command("ls", quiet=True)).decode().splitlines()
        self.managed = True
        if self.name in names:
            return
        await self.command(
            "create",
            "shell",
            name=self.name,
            template=self.config.image,
            pull="missing",
            cpus=self.config.cpus,
            memory=f"{self.config.memory_mib}m",
            skills="off",
            no_progress=True,
            env=[
                f"OCTOMATE_SANDBOX_USER_ID={self.user_id}",
                f"OCTOMATE_SANDBOX_IMAGE={self.config.image}",
                f"OCTOMATE_SANDBOX_CPUS={self.config.cpus}",
                f"OCTOMATE_SANDBOX_MEMORY_MIB={self.config.memory_mib}",
            ],
        )

    async def open(self) -> UNIXSocketStream:
        """Connect guest stdio directly to a byte stream, bypassing sh buffering."""
        parent, child = socket.socketpair()
        stream: UNIXSocketStream | None = None
        try:
            stream = await UNIXSocketStream.from_socket(parent)
            with child.makefile("rwb", buffering=0) as stdio:
                process = self.cli().bake(
                    "exec", interactive=True, workdir="/home/agent/workspace"
                )(
                    self.name,
                    "/opt/octomate/.venv/bin/python",
                    "-m",
                    "octomate.sandbox.entrypoint",
                    _in=stdio,
                    _out=stdio.fileno(),
                )
            self.process = process
            return stream
        except BaseException as error:
            if stream is not None:
                await stream.aclose()
            else:
                parent.close()
            if isinstance(error, (OSError, sh.ForkException)):
                raise SandboxError("Cannot attach to the sandbox agent") from None
            raise
        finally:
            child.close()

    async def stop(self) -> None:
        if self.managed:
            names = (await self.command("ls", quiet=True)).decode().splitlines()
            if self.name in names:
                await self.command("stop", self.name)
        process = self.process
        if process is None:
            return
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
        self.process = None
