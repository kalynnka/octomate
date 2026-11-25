"""Explicit local VM acceptance of the owner's mounted storage root."""

import json
import os
from contextlib import AsyncExitStack
from shlex import quote

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import HabitatManager
from octomate.nerve import Nerve
from octomate.schemas.habitat import InvocationScope
from tests.integration.sbx import remove_sandbox
from tests.support.managers import a_thread, a_workspace_manager
from tests.support.users import a_user


@pytest.mark.timeout(600)
async def test_guest_writes_land_in_the_owners_volumes_and_nowhere_else(
    in_memory_engine: AsyncEngine,
    sbx_cleanup: AsyncExitStack,
) -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    if not image:
        pytest.fail("Set SBX_TEST_IMAGE to the immutable SDK image reference")
    owner, other = await a_user("owner"), await a_user("other")
    workspaces = a_workspace_manager()
    manager = HabitatManager(SbxHabitatConfig(image=image), Nerve(), workspaces)
    scope = InvocationScope(user_id=owner.id, conversation_id=uuid7())
    sandbox = manager.residence(scope).habitat
    assert isinstance(sandbox, SbxHabitat)
    try:
        async with (
            workspaces.open(await a_thread("theirs"), None, other.id) as theirs,
            workspaces.open(await a_thread(), None, owner.id) as workspace,
            manager.enter(scope, uuid7()),
        ):
            cache = workspace.volumes.get("cache")
            config = workspace.volumes.get("config")
            mirror = await workspace.volume.mirror
            assert cache is not None
            assert config is not None
            assert mirror is not None
            # The kit puts the agent's credential file in the configuration
            # volume, holding only the proxy's placeholder tokens.
            credential = json.loads(
                (config.path / ".claude" / ".credentials.json").read_text()
            )
            assert credential["claudeAiOauth"]["accessToken"] == (
                "sk-ant-oat01-proxy-managed"
            )
            assert mirror.path.is_dir()
            await sandbox.control(
                "exec",
                "--workdir",
                str(sandbox.path(workspace.volume)),
                sandbox.name,
                "sh",
                "-c",
                f"printf tree > written.txt && "
                f"printf cache > {quote(str(sandbox.path(cache) / 'written.txt'))}",
            )
            assert (workspace.path / "written.txt").read_text() == "tree"
            assert (cache.path / "written.txt").read_text() == "cache"
            # A volume made after the sandbox exists is seen through the same mount.
            async with workspaces.open(
                await a_thread("later"), None, owner.id
            ) as later:
                (later.path / "host.txt").write_text("later")
                seen = await sandbox.control(
                    "exec",
                    sandbox.name,
                    "cat",
                    str(sandbox.path(later.volume) / "host.txt"),
                )
                assert seen == b"later"
            hidden = await sandbox.control(
                "exec",
                sandbox.name,
                "sh",
                "-c",
                f"test -e {quote(str(mirror.path))} || "
                f"test -e {quote(str(theirs.path))} || printf hidden",
            )
            assert hidden == b"hidden"
    finally:
        for state in manager.residences.values():
            backend = state.habitat
            if isinstance(backend, SbxHabitat) and backend.managed:
                sbx_cleanup.push_async_callback(remove_sandbox, backend)
        sbx_cleanup.push_async_callback(manager.close)
