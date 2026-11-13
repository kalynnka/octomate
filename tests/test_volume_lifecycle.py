"""Logical volumes own content lifecycle over an explicitly supplied host backing."""

from contextlib import AsyncExitStack
from pathlib import Path

import anyio
import pytest
from anyio.lowlevel import checkpoint
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers.workspaces.mirrors import MirrorManager, run_git
from octomate.schemas.volumes import GitVolume, Volume
from octomate.schemas.volumes.backings import HostDirectory
from tests.support.users import a_user


async def test_loaded_volume_prepares_and_retains_its_backing(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> None:
    owner = await a_user()
    volume = Volume(owner_id=owner.id, usage="cache")
    async with async_session() as session:
        session.add(volume)
        await session.commit()
    async with async_session() as session:
        loaded = await session.one_or_none(
            Volume, expressions=[Volume["id"] == volume.id]
        )
    assert loaded is not None
    backing = HostDirectory(tmp_path / "cache")
    assert not backing.path.exists()

    async with loaded(backing) as path:
        assert path.is_dir()
        assert backing.uses == 1
        (path / "package.whl").write_text("cached")

    assert backing.uses == 0
    assert (backing.path / "package.whl").read_text() == "cached"


async def test_overlapping_temporary_uses_release_only_after_the_last_exit(
    tmp_path: Path,
) -> None:
    volume = Volume(owner_id=uuid7(), ephemeral=True)
    reloaded = Volume.model_validate(volume.model_dump())
    backing = HostDirectory(tmp_path / "scratch")
    # The later use ends after the earlier one; these lifetimes are not nested.
    async with AsyncExitStack() as later:
        async with volume(backing) as path:
            (path / "work").write_text("in use")
            await later.enter_async_context(reloaded(backing))
            assert backing.uses == 2
        assert backing.uses == 1
        assert (path / "work").read_text() == "in use"
    assert backing.uses == 0
    assert not backing.path.exists()


@pytest.mark.parametrize("ephemeral", [False, True])
async def test_cancelled_directory_use_balances_claim_and_respects_retention(
    tmp_path: Path, ephemeral: bool
) -> None:
    volume = Volume(owner_id=uuid7(), ephemeral=ephemeral)
    backing = HostDirectory(tmp_path / "directory")
    with anyio.CancelScope() as scope:
        async with volume(backing) as path:
            (path / "work").write_text("unfinished")
            scope.cancel()
            await checkpoint()
    assert backing.uses == 0
    assert backing.path.exists() is not ephemeral


async def test_unscoped_git_volume_checkpoints_and_restores_without_a_workspace(
    tmp_path: Path,
) -> None:
    volume = GitVolume(owner_id=uuid7())
    backing = HostDirectory(tmp_path / "git")
    mirror = await MirrorManager(mirrors_dir=tmp_path / "mirrors").create()
    async with volume(backing, mirror=mirror) as path:
        assert (
            await run_git("branch", "--show-current", cwd=path)
        ).strip() == volume.branch
        (path / "work").write_text("saved")
    assert backing.uses == 0
    assert await run_git("show", f"{volume.snapshot_ref}:work", cwd=mirror) == "saved"

    await backing.release()
    async with volume(backing, mirror=mirror) as restored:
        assert (restored / "work").read_text() == "saved"


async def test_git_entry_without_a_source_fails_before_claiming_or_creating_files(
    tmp_path: Path,
) -> None:
    volume = GitVolume(owner_id=uuid7())
    backing = HostDirectory(tmp_path / "git")
    with pytest.raises(ValueError, match="requires a source mirror"):
        async with volume(backing):
            pytest.fail("An unprepared Git volume was entered")
    assert backing.uses == 0
    assert not backing.path.exists()
