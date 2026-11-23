from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tomllib
from contextlib import suppress
from pathlib import Path

import anyio
import pytest
import yaml
from anyio.streams.buffered import BufferedByteReceiveStream
from pydantic import BaseModel, HttpUrl
from uuid_utils.compat import uuid7

import octomate.habitat.sbx as sbx_module
from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat import HabitatError
from octomate.habitat.sbx import SbxHabitat
from tests.test_habitat_contracts import IMAGE


class ProcessInfo(BaseModel):
    pid: int
    argv: list[str]
    environment: dict[str, str]
    tty: list[bool]


@pytest.mark.parametrize("mode", ["oauth", "apikey", "none", None, "invalid"])
def test_kit_initializes_codex_auth_and_clears_previous_mode_settings(
    tmp_path: Path, mode: str | None
) -> None:
    kit_directory = Path(sbx_module.__file__).parent
    kit = yaml.safe_load((kit_directory / "spec.yaml").read_text())
    home = tmp_path / "home"
    shutil.copytree(kit_directory / "files" / "home", home)
    owner_config = tmp_path / "owner's config"
    codex_home = owner_config / ".codex"
    codex_home.mkdir(parents=True)
    config = codex_home / "config.toml"
    auth = codex_home / "auth.json"
    config.write_text('model_provider = "previous"\n')
    auth.write_text('{"OPENAI_API_KEY":"previous-placeholder"}')
    session = codex_home / "sessions" / "retained.jsonl"
    session.parent.mkdir()
    session.write_text("retained history\n")
    environment = {
        "PATH": os.defpath,
        "HOME": str(home),
        "OCTOMATE_HABITAT_CONFIG": str(owner_config),
    }
    if mode is not None:
        environment["SBX_CRED_OPENAI_MODE"] = mode
    for hook in kit["setup"]["install"]:
        result = subprocess.run(
            ["sh", "-c", hook["command"]],
            env=environment,
            capture_output=True,
            text=True,
        )
        assert result.returncode == (1 if mode == "invalid" else 0), result.stderr
    assert session.read_text() == "retained history\n"
    settings = tomllib.loads(config.read_text())
    if mode == "invalid":
        assert settings == {"model_provider": "previous"}
        assert json.loads(auth.read_text()) == {
            "OPENAI_API_KEY": "previous-placeholder"
        }
        return
    if mode not in {"oauth", "apikey"}:
        assert settings == {}
        assert not auth.exists()
        return
    assert json.loads(auth.read_text()) == {"OPENAI_API_KEY": "proxy-managed"}
    assert auth.stat().st_mode & 0o777 == 0o600
    assert settings == {
        "forced_login_method": "api",
        **(
            {
                "model_provider": "sandboxd",
                "model_providers": {
                    "sandboxd": {
                        "name": "Sandbox Proxy",
                        "base_url": "https://chatgpt.com/backend-api/codex",
                        "experimental_bearer_token": "oai-oat01-proxy-managed",
                        "requires_openai_auth": False,
                    }
                },
            }
            if mode == "oauth"
            else {}
        ),
    }


@pytest.fixture
def sandbox(tmp_path: Path) -> SbxHabitat:
    executable = tmp_path / "fake sbx"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if args[1] == 'many':\n"
        "    print('\\n'.join(map(str, range(100))))\n"
        "    sys.exit(0)\n"
        "if args[1] == 'fail':\n"
        "    print('synthetic-secret', file=sys.stderr)\n"
        "    print('synthetic-secret')\n"
        "    sys.exit(7)\n"
        "if args[1] == 'wait':\n"
        "    Path(args[2]).write_text(str(os.getpid()))\n"
        "    print('started', flush=True)\n"
        "    signal.pause()\n"
        "print(json.dumps(dict(pid=os.getpid(), argv=args, environment=dict(os.environ), "
        "tty=[os.isatty(0), os.isatty(1)])), flush=True)\n"
        "if args[1] == 'exec':\n"
        "    for line in sys.stdin.buffer:\n"
        "        sys.stdout.buffer.write(line)\n"
        "        sys.stdout.buffer.flush()\n"
    )
    executable.chmod(0o755)
    return SbxHabitat(
        config=SbxHabitatConfig(image=IMAGE, executable=str(executable)),
        user_id=uuid7(),
        root=tmp_path / "root",
        config_dir=tmp_path / "root" / "config",
    )


def test_core_address_uses_the_host_port_and_refuses_offline_policy(
    sandbox: SbxHabitat,
) -> None:
    sandbox.config = sandbox.config.model_copy(
        update={"core_address": HttpUrl("http://192.0.2.10:8123")}
    )
    assert sandbox.core_url == "http://host.docker.internal:8123"
    sandbox.config = sandbox.config.model_copy(update={"internet_access": False})
    with pytest.raises(HabitatError, match="deny-all network policy"):
        _ = sandbox.core_url


@pytest.mark.parametrize("internet_access", [True, False])
async def test_sh_preserves_named_arguments_and_filters_host_environment(
    sandbox: SbxHabitat, monkeypatch: pytest.MonkeyPatch, internet_access: bool
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-secret")
    monkeypatch.setenv("OCTOMATE_DB_URL", "synthetic-secret")
    name = "literal spaces; $(false)"
    result = ProcessInfo.model_validate_json(
        await sandbox.command(
            "create",
            "/kit",
            kit_arg=["image=literal spaces; $(false)", "id=$literal"],
            deny_network=[] if internet_access else ["**"],
            name=name,
            pull="missing",
            skills="off",
            no_progress=True,
        )
    )
    assert result.argv == [
        "--debug=false",
        "create",
        "/kit",
        "--kit-arg=image=literal spaces; $(false)",
        "--kit-arg=id=$literal",
        *([] if internet_access else ["--deny-network=**"]),
        f"--name={name}",
        "--pull=missing",
        "--skills=off",
        "--no-progress",
    ]
    assert "ANTHROPIC_API_KEY" not in result.environment
    assert "OCTOMATE_DB_URL" not in result.environment
    assert result.tty == [False, False]


async def test_sh_attachment_preserves_duplex_bytes_and_eof(
    sandbox: SbxHabitat, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-secret")
    async with sandbox.execute(
        "python",
        "-c",
        "literal spaces; $(false)",
        workdir="/workspace",
        env={"INVOCATION": "literal value"},
    ) as stream:
        receive = BufferedByteReceiveStream(stream)
        async with asyncio.timeout(5):
            info = ProcessInfo.model_validate_json(
                await receive.receive_until(b"\n", 65536)
            )
            assert info.tty == [False, False]
            assert "ANTHROPIC_API_KEY" not in info.environment
            assert "OCTOMATE_DB_URL" not in info.environment
            assert info.argv == [
                "--debug=false",
                "exec",
                "--interactive",
                "--workdir=/workspace",
                "--env=INVOCATION=literal value",
                sandbox.name,
                "python",
                "-c",
                "literal spaces; $(false)",
            ]
            for payload in (b"first", b"\x00\xff\rsecond"):
                await stream.send(payload + b"\n")
                assert await receive.receive_until(b"\n", 65536) == payload
            await stream.send_eof()
            with pytest.raises(anyio.EndOfStream):
                await receive.receive()
    with pytest.raises(ProcessLookupError):
        os.kill(info.pid, 0)


async def test_sh_failure_keeps_stderr_in_diagnostics(
    sandbox: SbxHabitat, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(HabitatError, match=r"failed \(exit 7\)") as failure:
        await sandbox.command("fail")
    assert "synthetic-secret" not in str(failure.value)
    assert "synthetic-secret" in caplog.text


async def test_command_stdout_is_not_limited_by_the_stderr_tail(
    sandbox: SbxHabitat,
) -> None:
    output = await sandbox.command("many")
    assert output.decode().splitlines() == [str(index) for index in range(100)]


@pytest.mark.parametrize("cancel", [False, True])
async def test_command_context_reaps_its_child_without_stopping_the_vm(
    sandbox: SbxHabitat, cancel: bool
) -> None:
    started: asyncio.Future[ProcessInfo] = asyncio.Future()

    async def run() -> None:
        async with sandbox.execute("cat", workdir="/workspace") as stream:
            receive = BufferedByteReceiveStream(stream)
            started.set_result(
                ProcessInfo.model_validate_json(
                    await receive.receive_until(b"\n", 65536)
                )
            )
            if cancel:
                await asyncio.Future[None]()

    task = asyncio.create_task(run())
    info = await asyncio.wait_for(started, 5)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(info.pid, 0)


async def test_concurrent_commands_have_independent_lifetimes(
    sandbox: SbxHabitat,
) -> None:
    async with sandbox.execute("cat", workdir="/workspace") as first:
        receive = BufferedByteReceiveStream(first)
        first_info = ProcessInfo.model_validate_json(
            await receive.receive_until(b"\n", 65536)
        )
        async with sandbox.execute("cat", workdir="/workspace") as second:
            second_info = ProcessInfo.model_validate_json(
                await BufferedByteReceiveStream(second).receive_until(b"\n", 65536)
            )
            assert first_info.pid != second_info.pid
        with pytest.raises(ProcessLookupError):
            os.kill(second_info.pid, 0)
        await first.send(b"still running\n")
        assert await receive.receive_until(b"\n", 65536) == b"still running"
    with pytest.raises(ProcessLookupError):
        os.kill(first_info.pid, 0)


@pytest.mark.parametrize("cancel", [False, True])
async def test_sh_timeout_and_cancellation_reap_child(
    sandbox: SbxHabitat, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    monkeypatch.setattr(sbx_module, "COMMAND_TIMEOUT", 5 if cancel else 0.3)
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    command = sandbox.cli().bake(
        _out=lambda line: loop.call_soon_threadsafe(started.set)
    )
    monkeypatch.setattr(sandbox, "cli", lambda: command)
    pid_file = tmp_path / "child.pid"
    task = asyncio.create_task(sandbox.command("wait", str(pid_file)))
    try:
        async with asyncio.timeout(5):
            await started.wait()
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(HabitatError, match="timed out"):
                    await task
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError, HabitatError):
            await task


@pytest.mark.parametrize("attach", [False, True])
async def test_missing_sbx_has_an_actionable_error(
    tmp_path: Path, attach: bool
) -> None:
    sandbox = SbxHabitat(
        config=SbxHabitatConfig(image=IMAGE, executable=str(tmp_path / "missing")),
        user_id=uuid7(),
        root=tmp_path / "root",
        config_dir=tmp_path / "root" / "config",
    )
    if attach:
        with pytest.raises(HabitatError, match="configured executable"):
            async with sandbox.execute("cat", workdir="/workspace"):
                pytest.fail("missing executable launched")
    else:
        with pytest.raises(HabitatError, match="configured executable"):
            await sandbox.command("version")
