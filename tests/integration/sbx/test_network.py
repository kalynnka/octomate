"""Opt-in verification of kit resources and per-sandbox outbound TCP policy."""

import asyncio
import os
from contextlib import AsyncExitStack

import pytest
from pydantic import TypeAdapter
from uuid_utils.compat import uuid7

from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import HabitatManager
from octomate.nerve import Nerve
from octomate.schemas.habitat import InvocationScope
from octomate.types.json import JsonObject
from tests.integration.sbx import remove_sandbox
from tests.support.managers import a_workspace_manager


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    ("internet_access", "denied_domains"),
    [(True, []), (False, []), (True, ["example.com:443"])],
)
async def test_internet_access_is_applied_at_creation(
    internet_access: bool, denied_domains: list[str], sbx_cleanup: AsyncExitStack
) -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    if not image:
        pytest.fail("Set SBX_TEST_IMAGE to the immutable SDK image reference")
    manager = HabitatManager(
        SbxHabitatConfig(
            image=image,
            cpus=1,
            memory_mib=1024,
            internet_access=internet_access,
            denied_domains=denied_domains,
        ),
        Nerve(),
        a_workspace_manager(),
    )
    scope = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    sandbox = manager.residence(scope).habitat
    assert isinstance(sandbox, SbxHabitat)
    assert (
        sandbox.id
        not in (await sandbox.control("ls", quiet=True)).decode().splitlines()
    )
    try:
        async with manager.enter(scope, uuid7()):
            await sandbox.control(
                "exec",
                sandbox.id,
                "python",
                "-c",
                "import os; from pathlib import Path; "
                "assert os.cpu_count() == 1; "
                "memory = next(line for line in Path('/proc/meminfo').read_text().splitlines() "
                "if line.startswith('MemTotal:')); "
                "assert 768 < int(memory.split()[1]) / 1024 <= 1024, memory",
            )
            for host in ("example.com:443", "api.anthropic.com:443"):
                async with (
                    asyncio.timeout(30),
                    sandbox.cli(
                        "policy",
                        "check",
                        "network",
                        "--json",
                        "--sandbox",
                        sandbox.id,
                        host,
                    ) as process,
                ):
                    stdout, stderr = await process.communicate()
                assert process.returncode in (0, 1), stderr.decode()
                policy = TypeAdapter(JsonObject).validate_json(stdout)
                assert policy["allowed"] is (
                    internet_access and host not in denied_domains
                )
                if not internet_access or host in denied_domains:
                    assert policy["deny_kind"] == "explicit"
            status = await sandbox.control(
                "exec",
                sandbox.id,
                "python",
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
            assert status.strip() == (
                b"200" if internet_access and not denied_domains else b"403"
            )
    finally:
        for state in manager.residences.values():
            backend = state.habitat
            if isinstance(backend, SbxHabitat) and backend.managed:
                sbx_cleanup.push_async_callback(remove_sandbox, backend)
        sbx_cleanup.push_async_callback(manager.close)
