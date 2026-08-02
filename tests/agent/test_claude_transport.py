from __future__ import annotations

from claude_agent_sdk import ClaudeAgentOptions

from octomate.config.agents import ClaudeSSHConfig
from octomate.tentacles.agent.claude.transport import SSHTransport


def _options(**kwargs: object) -> ClaudeAgentOptions:
    base: dict[str, object] = {"cwd": "/srv/proj", "model": "opus"}
    base.update(kwargs)
    return ClaudeAgentOptions(**base)  # type: ignore[arg-type]


def test_build_command_wraps_the_sdk_command_in_ssh() -> None:
    ssh = ClaudeSSHConfig(host="user@box")
    transport = SSHTransport("hi", _options(resume="sess-9", max_turns=7), ssh=ssh)

    cmd = transport._build_command()

    assert cmd[:2] == ["ssh", "-T"]
    assert cmd[-2] == "user@box"
    remote = cmd[-1]
    # The remote binary is argv[0] and the SDK's full option mapping is preserved.
    assert "claude --output-format stream-json" in remote
    assert "--model opus" in remote
    assert "--resume sess-9" in remote
    assert "--max-turns 7" in remote
    assert "--input-format stream-json" in remote
    assert "cd /srv/proj && export " in remote


def test_local_cwd_is_cleared_and_remote_binary_used() -> None:
    ssh = ClaudeSSHConfig(host="box", claude_bin="/opt/claude/bin/claude")
    transport = SSHTransport("hi", _options(cwd="/remote/only"), ssh=ssh)

    # ssh must not be chdir'd into a remote-only path locally...
    assert transport._cwd is None
    assert transport.remote_cwd == "/remote/only"
    # ...and the remote binary name is argv[0] of the wrapped command.
    assert transport._cli_path == "/opt/claude/bin/claude"
    assert "/opt/claude/bin/claude --output-format" in transport._build_command()[-1]


def test_tilde_cwd_expands_on_the_remote() -> None:
    transport = SSHTransport(
        "hi", _options(cwd="~/work/repo"), ssh=ClaudeSSHConfig(host="box")
    )
    assert 'cd "$HOME"/work/repo && ' in transport._build_command()[-1]


def test_identity_file_and_ssh_options_are_threaded() -> None:
    ssh = ClaudeSSHConfig(
        host="box", identity_file="/keys/id_ed25519", ssh_options=["-p", "2222"]
    )
    cmd = SSHTransport("hi", _options(), ssh=ssh)._build_command()

    assert "-i" in cmd
    assert cmd[cmd.index("-i") + 1] == "/keys/id_ed25519"
    assert "-p" in cmd
    assert cmd[cmd.index("-p") + 1] == "2222"


async def _can_use_tool(*_a: object, **_k: object) -> None:
    return None


def test_can_use_tool_enables_the_stdio_permission_protocol() -> None:
    # The client applies the stdio permission tool to its own transport, not a
    # custom one, so the SSH transport must add it when can_use_tool is set.
    with_cb = SSHTransport(
        "hi", _options(can_use_tool=_can_use_tool), ssh=ClaudeSSHConfig(host="box")
    )
    assert "--permission-prompt-tool stdio" in with_cb._build_command()[-1]

    without_cb = SSHTransport("hi", _options(), ssh=ClaudeSSHConfig(host="box"))
    assert "--permission-prompt-tool" not in without_cb._build_command()[-1]
