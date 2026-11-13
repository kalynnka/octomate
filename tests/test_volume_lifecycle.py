"""Logical volumes own content lifecycle over their recorded host storage."""

import json
from contextlib import AsyncExitStack
from pathlib import Path

import anyio
import pytest
from anyio.lowlevel import checkpoint
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers.workspaces.mirrors import MirrorManager, run_git
from octomate.schemas.volumes import GitVolume, Volume
from octomate.schemas.volumes.storage import HostDirectory
from tests.support.managers import FakeVolumeManager
from tests.support.users import a_user


def test_storage_schema_exposes_only_persisted_location() -> None:
    schema = Volume.model_json_schema()["$defs"]["HostDirectory"]
    assert set(schema["properties"]) == {"path", "kind"}


async def test_loaded_volume_prepares_and_retains_its_storage(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> None:
    owner = await a_user()
    storage = HostDirectory(tmp_path / "cache")
    volume = Volume(owner_id=owner.id, usage="cache", storage=storage)
    async with async_session() as session:
        session.add(volume)
        await session.commit()
    async with async_session() as session:
        loaded = await session.one_or_none(
            Volume, expressions=[Volume["id"] == volume.id]
        )
    assert loaded is not None
    storage = loaded.storage
    assert not storage.path.exists()

    async with loaded() as path:
        assert path.is_dir()
        assert storage.uses == 1
        (path / "package.whl").write_text("cached")
        async with async_session() as session:
            session.add(loaded)
            await session.commit()
        async with in_memory_engine.connect() as connection:
            payload = await connection.scalar(text("SELECT storage FROM volumes"))
        assert json.loads(payload) == {"kind": "host-directory", "path": str(path)}

    assert storage.uses == 0
    assert (storage.path / "package.whl").read_text() == "cached"


async def test_overlapping_temporary_uses_release_only_after_the_last_exit(
    tmp_path: Path,
) -> None:
    storage = HostDirectory(tmp_path / "scratch")
    volume = Volume(owner_id=uuid7(), ephemeral=True, storage=storage)
    reloaded = Volume.model_validate(volume.model_dump())
    reloaded.storage = storage
    # The later use ends after the earlier one; these lifetimes are not nested.
    async with AsyncExitStack() as later:
        async with volume() as path:
            (path / "work").write_text("in use")
            await later.enter_async_context(reloaded())
            assert storage.uses == 2
        assert storage.uses == 1
        assert (path / "work").read_text() == "in use"
    assert storage.uses == 0
    assert not storage.path.exists()


@pytest.mark.parametrize("ephemeral", [False, True])
async def test_cancelled_directory_use_balances_claim_and_respects_retention(
    tmp_path: Path, ephemeral: bool
) -> None:
    storage = HostDirectory(tmp_path / "directory")
    volume = Volume(owner_id=uuid7(), ephemeral=ephemeral, storage=storage)
    with anyio.CancelScope() as scope:
        async with volume() as path:
            (path / "work").write_text("unfinished")
            scope.cancel()
            await checkpoint()
    assert storage.uses == 0
    assert storage.path.exists() is not ephemeral


async def test_unscoped_git_volume_checkpoints_and_restores_without_a_workspace(
    tmp_path: Path,
) -> None:
    storage = HostDirectory(tmp_path / "git")
    volume = GitVolume(owner_id=uuid7(), storage=storage)
    mirror = await MirrorManager(
        volumes=FakeVolumeManager(), mirrors_dir=tmp_path / "mirrors"
    ).create()
    volume.mirror.value = mirror
    async with volume() as path:
        assert (
            await run_git("branch", "--show-current", cwd=path)
        ).strip() == volume.branch
        (path / "work").write_text("saved")
    assert storage.uses == 0
    assert (
        await run_git("show", f"{volume.snapshot_ref}:work", cwd=mirror.storage.path)
        == "saved"
    )

    await storage.release()
    async with volume() as restored:
        assert (restored / "work").read_text() == "saved"


async def test_git_entry_without_a_source_fails_before_claiming_or_creating_files(
    tmp_path: Path,
) -> None:
    storage = HostDirectory(tmp_path / "git")
    volume = GitVolume(owner_id=uuid7(), storage=storage)
    with pytest.raises(ValueError, match="requires a source mirror"):
        async with volume():
            pytest.fail("An unprepared Git volume was entered")
    assert storage.uses == 0
    assert not storage.path.exists()


@pytest.mark.parametrize("path", ["../peer", "/volumes/../peer"])
def test_storage_rejects_parent_traversal(path: str) -> None:
    with pytest.raises(ValueError, match="parent traversal"):
        HostDirectory(Path(path))


async def test_host_mirror_entry_retains_its_contents_without_a_checkpoint(
    tmp_path: Path,
) -> None:
    mirror = await MirrorManager(
        volumes=FakeVolumeManager(), mirrors_dir=tmp_path / "mirrors"
    ).create()
    async with mirror() as path:
        assert mirror.storage.uses == 1
        (path / "host-only").write_text("retained")
    assert mirror.storage.uses == 0
    assert (path / "host-only").read_text() == "retained"
    assert await run_git("for-each-ref", "refs/octomate/trees", cwd=path) == ""
