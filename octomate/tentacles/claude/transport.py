from __future__ import annotations

import shlex
from collections.abc import AsyncIterable
from dataclasses import replace
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from claude_agent_sdk._version import __version__ as SDK_VERSION

from octomate.config.agents import ClaudeSSHConfig


class SSHTransport(SubprocessCLITransport):
    """Run the Claude CLI on a remote host over SSH.

    Subclasses the SDK's subprocess transport so the full option->flag mapping
    (`--model`, `--resume`, `--permission-mode`, `--allowedTools`, `--max-turns`,
    ...) and the stream-json control protocol are reused verbatim; only the
    *locus* of execution changes. `_build_command` wraps the SDK's own command in
    an `ssh` invocation of the remote `claude` binary; the local version probe is
    skipped (the CLI lives on the remote); and the local cwd is cleared so `ssh`
    is not launched inside a path that only exists on the remote.

    Couples to the SDK-private `SubprocessCLITransport` — pinned to `0.1.80`; an
    SDK bump touches only this file.
    """

    ssh: ClaudeSSHConfig
    remote_cwd: str | None

    def __init__(
        self,
        prompt: str | AsyncIterable[dict[str, Any]],
        options: ClaudeAgentOptions,
        *,
        ssh: ClaudeSSHConfig,
    ) -> None:
        # A `can_use_tool` callback routes permissions through the stdio control
        # protocol, which the CLI only enables with `--permission-prompt-tool
        # stdio`. The client applies this to its own transport but not to a
        # pre-constructed one (client.py `_connect_inner`), so apply it here.
        if options.can_use_tool and not options.permission_prompt_tool_name:
            options = replace(options, permission_prompt_tool_name="stdio")
        super().__init__(prompt, options)
        self.ssh = ssh
        # The CLI runs on the remote: use the remote binary as argv[0] (skips the
        # local `_find_cli`), and keep `options.cwd` only for the remote `cd` — the
        # ssh process itself must not chdir into a remote-only path.
        self._cli_path = ssh.claude_bin
        self.remote_cwd = self._cwd
        self._cwd = None

    async def _check_claude_version(self) -> None:
        # The CLI lives on the remote host; there is nothing local to version-check.
        return None

    def _build_command(self) -> list[str]:
        remote_argv = super()._build_command()  # [claude_bin, --output-format, ...]
        env = {
            "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
            "CLAUDE_AGENT_SDK_VERSION": SDK_VERSION,
            **self._options.env,
        }
        exports = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
        cwd = self.remote_cwd or "."
        # Expand a leading ~ on the remote (shlex.quote would wrap it and defeat
        # the shell's tilde expansion).
        cwd_expr = (
            '"$HOME"' + shlex.quote(cwd[1:])
            if cwd.startswith("~")
            else shlex.quote(cwd)
        )
        remote = f"cd {cwd_expr} && export {exports} && " + " ".join(
            shlex.quote(arg) for arg in remote_argv
        )
        ssh_argv = [
            "ssh",
            "-T",  # no TTY allocation — essential for piped stream-json
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if self.ssh.identity_file:
            ssh_argv += ["-i", self.ssh.identity_file]
        ssh_argv += self.ssh.ssh_options
        ssh_argv += [self.ssh.host, remote]
        return ssh_argv
