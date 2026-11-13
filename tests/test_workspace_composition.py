from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, nullcontext
from dataclasses import field
from pathlib import Path

import anyio
import pytest
from anyio.lowlevel import checkpoint
from arcanus import Relation
from pydantic.dataclasses import dataclass
from uuid_utils.compat import uuid7

from octomate.managers.workspaces.mirrors import MirrorManager, run_git
from octomate.schemas.volumes import GitVolume, Volume
from octomate.schemas.volumes.storage import HostDirectory
from octomate.schemas.workspaces import Workspace
from tests.support.managers import FakeVolumeManager


@dataclass
class RecordedDirectory(HostDirectory):
    events: list[str] = field(default_factory=list)

    @asynccontextmanager
    async def acquire(self, *, ephemeral: bool) -> AsyncGenerator[Path]:
        async with super().acquire(ephemeral=ephemeral) as path:
            self.events.append(f"enter {path.name}")
            try:
                yield path
            finally:
                self.events.append(f"exit {path.name}")


@pytest.mark.parametrize("fail", [False, True])
async def test_workspace_enters_all_volumes_and_unwinds_in_reverse_order(
    tmp_path: Path, fail: bool
) -> None:
    owner_id = uuid7()
    events: list[str] = []
    work = RecordedDirectory(tmp_path / "work")
    cache = RecordedDirectory(tmp_path / "cache")
    work.events = cache.events = events
    workspace = Workspace(
        thread_id=uuid7(),
        volumes={
            "work": Volume(owner_id=owner_id, storage=work, ephemeral=True),
            "cache": Volume(owner_id=owner_id, storage=cache, usage="cache"),
        },
    )
    outcome = pytest.raises(RuntimeError, match="run failed") if fail else nullcontext()
    with outcome:
        async with workspace() as prepared:
            assert prepared is workspace
            assert prepared.path == work.path
            assert work.uses == cache.uses == 1
            (work.path / "temporary").touch()
            (cache.path / "package").touch()
            if fail:
                raise RuntimeError("run failed")
    assert events == ["enter work", "enter cache", "exit cache", "exit work"]
    assert work.uses == cache.uses == 0
    assert not work.path.exists()
    assert (cache.path / "package").exists()


async def test_partial_preparation_releases_earlier_volumes(tmp_path: Path) -> None:
    owner_id = uuid7()
    work = HostDirectory(tmp_path / "work")
    broken = HostDirectory(tmp_path / "broken")
    broken.path.write_text("not a directory")
    workspace = Workspace(
        thread_id=uuid7(),
        volumes={
            "work": Volume(owner_id=owner_id, storage=work, ephemeral=True),
            "cache": Volume(owner_id=owner_id, storage=broken),
        },
    )
    with pytest.raises(FileExistsError):
        async with workspace():
            pytest.fail("A partially prepared workspace must not be yielded")
    assert work.uses == broken.uses == 0
    assert not work.path.exists()


async def test_cancellation_releases_all_volumes(tmp_path: Path) -> None:
    owner_id = uuid7()
    work = HostDirectory(tmp_path / "work")
    cache = HostDirectory(tmp_path / "cache")
    workspace = Workspace(
        thread_id=uuid7(),
        volumes={
            "work": Volume(owner_id=owner_id, storage=work, ephemeral=True),
            "cache": Volume(owner_id=owner_id, storage=cache),
        },
    )
    with anyio.CancelScope() as scope:
        async with workspace():
            (cache.path / "package").touch()
            scope.cancel()
            await checkpoint()
    assert work.uses == cache.uses == 0
    assert not work.path.exists()
    assert (cache.path / "package").exists()


async def test_git_volume_checkpoints_inside_a_composed_workspace(
    tmp_path: Path,
) -> None:
    owner_id = uuid7()
    work = HostDirectory(tmp_path / "work")
    cache = HostDirectory(tmp_path / "cache")
    mirror = await MirrorManager(
        volumes=FakeVolumeManager(), mirrors_dir=tmp_path / "mirrors"
    ).create()
    volume = GitVolume(
        owner_id=owner_id, storage=work, mirror_id=mirror.id, mirror=Relation(mirror)
    )
    workspace = Workspace(
        thread_id=uuid7(),
        volumes={"work": volume, "cache": Volume(owner_id=owner_id, storage=cache)},
    )
    async with workspace():
        (workspace.path / "work.txt").write_text("saved")
    assert (
        await run_git(
            "show", f"{volume.snapshot_ref}:work.txt", cwd=mirror.storage.path
        )
        == "saved"
    )
    assert work.uses == cache.uses == 0


def test_workspace_selects_a_working_volume(tmp_path: Path) -> None:
    storage = HostDirectory(tmp_path / "source")
    workspace = Workspace(
        thread_id=uuid7(),
        directory="source",
        volumes={"source": Volume(owner_id=uuid7(), storage=storage)},
    )
    assert workspace.path == storage.path
    assert not storage.path.exists()


def test_workspace_rejects_missing_working_directory() -> None:
    with pytest.raises(ValueError, match="working directory"):
        Workspace(
            thread_id=uuid7(),
            volumes={},
        )


def test_workspace_rejects_host_mirrors(tmp_path: Path) -> None:
    mirror = GitVolume(usage="mirror", storage=HostDirectory(tmp_path / "mirror"))
    with pytest.raises(ValueError, match="Host mirrors"):
        Workspace(thread_id=uuid7(), volumes={"work": mirror})


def test_workspace_rejects_different_volume_owners(tmp_path: Path) -> None:
    work = HostDirectory(tmp_path / "work")
    other = HostDirectory(tmp_path / "other")
    with pytest.raises(ValueError, match="owners do not match"):
        Workspace(
            thread_id=uuid7(),
            volumes={
                "work": Volume(owner_id=uuid7(), storage=work),
                "other": Volume(owner_id=uuid7(), storage=other),
            },
        )
