"""Opt-in verification of kit resources and per-sandbox outbound TCP policy."""

import os

import pytest
from pydantic import TypeAdapter
from uuid_utils.compat import uuid7

from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import HabitatManager
from octomate.nerve import Nerve
from octomate.schemas.habitat import InvocationScope
from octomate.types.json import JsonObject
from tests.support.managers import a_workspace_manager


@pytest.mark.timeout(300)
@pytest.mark.parametrize("internet_access", [True, False])
async def test_internet_access_is_applied_at_creation(internet_access: bool) -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    if not image:
        pytest.fail("Set SBX_TEST_IMAGE to the immutable SDK image reference")
    manager = HabitatManager(
        SbxHabitatConfig(
            image=image,
            cpus=1,
            memory_mib=1024,
            internet_access=internet_access,
        ),
        Nerve(),
        a_workspace_manager(),
    )
    scope = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    sandbox = manager.residence(scope).habitat
    assert isinstance(sandbox, SbxHabitat)
    assert (
        sandbox.name
        not in (await sandbox.command("ls", quiet=True)).decode().splitlines()
    )
    try:
        async with manager.enter(scope, uuid7()):
            await sandbox.command(
                "exec",
                sandbox.name,
                "/opt/octomate/.venv/bin/python",
                "-c",
                "import os; from pathlib import Path; "
                "assert os.cpu_count() == 1; "
                "memory = next(line for line in Path('/proc/meminfo').read_text().splitlines() "
                "if line.startswith('MemTotal:')); "
                "assert 768 < int(memory.split()[1]) / 1024 <= 1024, memory",
            )
            for host in ("example.com:443", "api.anthropic.com:443"):
                process = sandbox.cli()(
                    "policy",
                    "check",
                    "network",
                    "--json",
                    "--sandbox",
                    sandbox.name,
                    host,
                    _ok_code=(0, 1),
                    _timeout=30,
                )
                await process
                policy = TypeAdapter(JsonObject).validate_json(process.stdout)
                assert policy["allowed"] is internet_access
                if not internet_access:
                    assert policy["deny_kind"] == "explicit"
            status = await sandbox.command(
                "exec",
                sandbox.name,
                "/opt/octomate/.venv/bin/python",
                "-c",
                "import urllib.error, urllib.request\n"
                "try:\n"
                "    with urllib.request.urlopen('https://example.com', timeout=15) as response:\n"
                "        print(response.status)\n"
                "except urllib.error.HTTPError as error:\n"
                "    print(error.code)\n"
                "except urllib.error.URLError as error:\n"
                "    assert str(error.reason) == 'Tunnel connection failed: 403 Forbidden', str(error.reason)\n"
                "    print(403)\n",
            )
            assert status.strip() == (b"200" if internet_access else b"403")
    finally:
        try:
            await manager.close()
        finally:
            if (
                sandbox.name
                in (await sandbox.command("ls", quiet=True)).decode().splitlines()
            ):
                await sandbox.command("rm", "--force", sandbox.name)
