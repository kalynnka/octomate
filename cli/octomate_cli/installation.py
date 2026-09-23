"""Prepared installations and the service definitions that launch them."""

from __future__ import annotations

import json
import os
import plistlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml


class DeploymentTarget(StrEnum):
    """How the operator will supervise the prepared server."""

    launchd = "launchd"
    systemd = "systemd"
    docker = "docker"
    manual = "manual"


@dataclass
class Installation:
    """Paths and process environment shared by preparation and validation."""

    directory: Path
    environment: dict[str, str]
    target: DeploymentTarget

    @property
    def draft(self) -> Path:
        names = {
            DeploymentTarget.launchd: "control/io.octomate.server.plist",
            DeploymentTarget.systemd: "control/octomate.service",
            DeploymentTarget.docker: "compose.yaml",
            DeploymentTarget.manual: "CONFIGURATION.md",
        }
        return self.directory / names[self.target]

    @property
    def state(self) -> Path:
        """Host directory containing config, secrets, workspaces and the database."""
        return (
            self.directory / "state"
            if self.target == DeploymentTarget.docker
            else self.directory
        )

    def maintenance_command(self, action: str) -> list[str]:
        """Invoke the same configuration generator in the chosen runtime."""
        return (
            [
                "docker",
                "compose",
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "octomate",
                "python",
            ]
            if self.target == DeploymentTarget.docker
            else [str(self.directory / "app/.venv/bin/python")]
        ) + ["-m", "octomate_cli.deployment", action]

    def write_definition(self, port: int) -> None:
        """Write a draft only; never register, enable or start a service."""
        if self.target == DeploymentTarget.manual:
            return
        if self.target == DeploymentTarget.launchd:
            payload = {
                "Label": "io.octomate.server",
                "WorkingDirectory": str(self.directory),
                "ProgramArguments": [
                    str(self.directory / "app/.venv/bin/octomate"),
                    "service",
                    "serve",
                ],
                "EnvironmentVariables": self.environment,
                "StandardOutPath": str(self.directory / "logs/stdout.log"),
                "StandardErrorPath": str(self.directory / "logs/stderr.log"),
                "LimitLoadToSessionType": "Aqua",
                "KeepAlive": True,
                "RunAtLoad": True,
                "ThrottleInterval": 10,
                "Umask": 63,
            }
            with self.draft.open("xb") as output:
                self.draft.chmod(0o600)
                plistlib.dump(payload, output)
            return
        if self.target == DeploymentTarget.systemd:
            # systemd expands % specifiers even inside quoted values; ExecStart also expands $.
            directory = json.dumps(str(self.directory), ensure_ascii=False).replace(
                "%", "%%"
            )
            executable = (
                json.dumps(
                    str(self.directory / "app/.venv/bin/octomate"), ensure_ascii=False
                )
                .replace("%", "%%")
                .replace("$", "$$")
            )
            environment = "\n".join(
                "Environment="
                + json.dumps(f"{key}={value}", ensure_ascii=False).replace("%", "%%")
                for key, value in self.environment.items()
            )
            contents = (
                "[Unit]\nDescription=Octomate\n\n"
                "[Service]\nType=simple\n"
                f"WorkingDirectory={directory}\n{environment}\n"
                f"ExecStart={executable} service serve\n"
                "Restart=on-failure\nRestartSec=10\nUMask=0077\n\n"
                "[Install]\nWantedBy=default.target\n"
            )
        else:
            # Keep the repository Compose file as the single Docker layout; paths are
            # relative to the generated file instead of the cloned source directory.
            compose = yaml.safe_load(
                (self.directory / "app/docker-compose.yml").read_text()
            )
            service = compose["services"]["octomate"]
            service["build"] = {
                "context": "./app",
                "args": {
                    "OCTOMATE_UID": str(os.getuid()),
                    "OCTOMATE_GID": str(os.getgid()),
                },
            }
            service["user"] = f"{os.getuid()}:{os.getgid()}"
            frontend = compose["services"]["trunkline"]
            frontend["build"]["context"] = "./app"
            frontend["ports"] = [f"127.0.0.1:{port}:8080"]
            contents = yaml.safe_dump(compose, sort_keys=False)
        with self.draft.open("x") as output:
            self.draft.chmod(0o600)
            output.write(contents)
