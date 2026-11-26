"""Opt-in, no-model checks against the pinned macOS SRT executable."""

import asyncio
import os
import signal
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import anyio
import pytest
from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite
from uuid_utils.compat import uuid7

from octomate.habitat.srt import SrtHabitat
from octomate.managers.habitat import Invocation
from octomate.schemas.conversation import Conversation
from octomate.schemas.volumes import Volume
from octomate.schemas.volumes.storage import HostDirectory
from octomate.schemas.workspaces import Workspace
from octomate.tentacles.inkling.workspace import HabitatWorkspaceBackend
from tests.habitat.test_commands import (
    test_cancellation_reaps_the_command as test_cancellation_reaps_the_command,
)
from tests.habitat.test_commands import (
    test_concurrent_runs_keep_their_directories_separate as test_concurrent_runs_keep_their_directories_separate,
)
from tests.habitat.test_commands import (
    test_invalid_command_is_rejected as test_invalid_command_is_rejected,
)
from tests.habitat.test_commands import (
    test_missing_program_returns_127 as test_missing_program_returns_127,
)
from tests.habitat.test_commands import (
    test_run_captures_output_exit_and_explicit_environment as test_run_captures_output_exit_and_explicit_environment,
)
from tests.habitat.test_commands import (
    test_run_timeout_retains_output as test_run_timeout_retains_output,
)


@pytest.fixture
def invocation(habitat: SrtHabitat, tmp_path: Path) -> Invocation:
    thread = uuid7()
    return Invocation(
        id=uuid7(),
        habitat=habitat,
        workspace=Workspace(
            thread_id=thread,
            volumes={
                "work": Volume(
                    owner_id=habitat.user_id, storage=HostDirectory(tmp_path)
                )
            },
        ),
        conversation=Conversation(thread_id=thread, agent_tentacle_id="inkling"),
    )


class TestWorkspace(WorkspaceBackendSuite):
    @pytest.fixture
    def backend(self, invocation: Invocation) -> HabitatWorkspaceBackend:
        return HabitatWorkspaceBackend(invocation)

    @pytest.fixture
    def attach_backend(
        self, invocation: Invocation
    ) -> Callable[[WorkspaceRef], WorkspaceBackend]:
        return lambda ref: HabitatWorkspaceBackend(invocation, ref=ref)

    @pytest.fixture
    def anyio_backend(self) -> str:
        return "asyncio"


async def test_peer_files_and_symlink_escapes_are_denied(
    habitat: SrtHabitat, tmp_path: Path
) -> None:
    peer = tmp_path.parent / f"peer-{uuid7()}"
    peer.mkdir()
    secret = peer / "secret"
    secret.write_text("synthetic-private-data")
    (tmp_path / "escape").symlink_to(peer, target_is_directory=True)
    result = await habitat.run(
        [
            "python",
            "-c",
            "from pathlib import Path; import sys; "
            "p=Path(sys.argv[1]); "
            "actions=[lambda:p.read_text(), lambda:p.write_text('changed'), "
            "lambda:Path('escape/new').write_text('escape')]; "
            "\nfor action in actions:\n try: action()\n except PermissionError: print('denied')\n",
            str(secret),
        ],
        workdir=str(tmp_path),
    )
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["denied"] * 3
    assert secret.read_text() == "synthetic-private-data"
    assert not (peer / "new").exists()


async def test_core_files_and_policy_are_unreadable(
    habitat: SrtHabitat, tmp_path: Path
) -> None:
    assert habitat.settings is not None
    for path in (
        Path(await anyio.Path(__file__).resolve()),
        Path(habitat.settings.name) / "settings.json",
        Path("/etc/passwd"),
        Path("/private/etc/passwd"),
    ):
        result = await habitat.run(["cat", str(path)], workdir=str(tmp_path))
        assert result.exit_code != 0
        assert not result.stdout


async def test_upstream_shared_temp_is_not_writable(
    habitat: SrtHabitat, tmp_path: Path
) -> None:
    path = Path("/private/tmp/claude") / f"octomate-probe-{uuid7()}"
    existed = await anyio.Path(path.parent).exists()
    try:
        result = await habitat.run(
            [
                "python",
                "-c",
                "from pathlib import Path; import sys; p=Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True); p.write_text('probe')",
                str(path),
            ],
            workdir=str(tmp_path),
        )
        assert result.exit_code != 0
        assert "Operation not permitted" in result.stderr
    finally:
        await anyio.Path(path).unlink(missing_ok=True)
        if not existed:
            with suppress(FileNotFoundError):
                await anyio.Path(path.parent).rmdir()


async def test_argv_is_literal_and_home_temp_are_private(
    habitat: SrtHabitat, tmp_path: Path
) -> None:
    value = "spaces 'quotes' $(touch injected); $HOME"
    result = await habitat.run(
        [
            "python",
            "-c",
            "import os,sys; print(sys.argv[1]); print(os.environ['HOME']); print(os.environ['TMPDIR'])",
            value,
        ],
        workdir=str(tmp_path),
    )
    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        value,
        str(tmp_path / "config/home"),
        str(tmp_path / "cache/tmp"),
    ]
    assert not (tmp_path / "injected").exists()


async def test_direct_loopback_is_denied(habitat: SrtHabitat, tmp_path: Path) -> None:
    server = await asyncio.start_server(
        lambda _reader, writer: writer.close(), "127.0.0.1", 0
    )
    async with server:
        port = server.sockets[0].getsockname()[1]
        result = await habitat.run(
            [
                "python",
                "-c",
                "import socket,sys; socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=1)",
                str(port),
            ],
            workdir=str(tmp_path),
        )
    assert result.exit_code != 0
    assert "Operation not permitted" in result.stderr


async def test_proxy_allows_unlisted_ports_and_blocks_the_blacklist(
    habitat: SrtHabitat, tmp_path: Path
) -> None:
    async def respond(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    first = await asyncio.start_server(respond, "127.0.0.1", 0)
    second = await asyncio.start_server(respond, "127.0.0.1", 0)
    async with first, second:
        allowed = first.sockets[0].getsockname()[1]
        denied = second.sockets[0].getsockname()[1]
        await habitat.stop()
        habitat.config = habitat.config.model_copy(
            update={"denied_domains": [f"127.0.0.1:{denied}"]}
        )
        await habitat.prepare()
        result = await habitat.run(
            [
                "python",
                "-c",
                "import httpx2,sys; "
                "\nwith httpx2.Client() as client:\n for port in sys.argv[1:]: print(client.get(f'http://127.0.0.1:{port}/').status_code)\n",
                str(allowed),
                str(denied),
            ],
            workdir=str(tmp_path),
            env={"NO_PROXY": "", "no_proxy": ""},
        )
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["200", "403"]


async def test_completed_command_reaps_background_children(
    habitat: SrtHabitat, tmp_path: Path
) -> None:
    result = await habitat.run(
        "sleep 60 >/dev/null 2>&1 & echo $!", shell=True, workdir=str(tmp_path)
    )
    assert result.exit_code == 0
    pid = int(result.stdout.strip())
    try:
        await habitat.stop()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
