"""Habitat paths are scoped to the explicitly attached storage."""

from pathlib import Path, PurePosixPath

import pytest

from octomate.habitat import Attachment, HabitatError
from octomate.schemas.volumes.storage import HostDirectory


@pytest.mark.parametrize("relative", ["", "notes.md", "cache/package", "a b"])
def test_guest_mapping_needs_only_the_storage_and_destination(
    tmp_path: Path, relative: str
) -> None:
    storage = HostDirectory(tmp_path / "storage" / "checkout")
    destination = PurePosixPath("/project")
    attachment = Attachment(storage, destination)

    mapped = attachment.path(storage.path / relative)

    assert mapped == destination / relative
    assert not isinstance(mapped, Path)
    assert not storage.path.exists()


def test_native_attachment_preserves_host_paths(tmp_path: Path) -> None:
    storage = HostDirectory(tmp_path / "tree")
    attachment = Attachment(storage, storage.path)
    mapped = attachment.path(storage.path / "work.txt")
    assert isinstance(mapped, Path)
    assert mapped == storage.path / "work.txt"


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (Path("/volumes/alice/tree/other/work"), "outside"),
        (Path("/volumes/alice/tree/id-extra/work"), "outside"),
        (Path("/volumes/bob/tree/id/work"), "outside"),
        (Path("/volumes/alice/tree/id/../other/work"), "traversal"),
        (Path("work"), "absolute"),
    ],
)
def test_mapping_refuses_paths_outside_the_attached_volume(
    path: Path, message: str
) -> None:
    attachment = Attachment(
        HostDirectory(Path("/volumes/alice/tree/id")), PurePosixPath("/project")
    )
    with pytest.raises(HabitatError, match=message):
        attachment.path(path)


@pytest.mark.parametrize(
    ("source", "destination", "message"),
    [
        (Path("/volumes/id"), PurePosixPath("project"), "absolute"),
        (Path("/volumes/id"), PurePosixPath("/project/../other"), "traversal"),
        (Path("/volumes/../id"), PurePosixPath("/project"), "traversal"),
    ],
)
def test_attachment_rejects_invalid_source_or_destination(
    source: Path, destination: PurePosixPath, message: str
) -> None:
    with pytest.raises(HabitatError, match=message):
        Attachment(HostDirectory(source), destination)
