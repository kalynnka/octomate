"""Host-directory allocation and shared runtime handles."""

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import UUID7

from octomate.managers.base import Manager
from octomate.schemas.volumes.storage import HostDirectory


@dataclass
class HostStorageManager(Manager):
    """Host storage layout and shared handles, resolved without creating contents."""

    root: Path = Path(
        ".octomate/volumes"
    )  # Host storage location for registered volumes.
    directories: dict[Path, HostDirectory] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.root = self.root.resolve()

    def get(self, path: Path) -> HostDirectory:
        """Reuse one lock and use count for every reference to this directory."""
        path = path.absolute()
        if path not in self.directories:
            self.directories[path] = HostDirectory(path)
        return self.directories[path]

    def temporary(self, thread_id: UUID7) -> HostDirectory:
        """An unregistered chat's stable temporary location, without a volume row."""
        return self.get(
            (
                Path(tempfile.gettempdir()) / "octomate-unregistered" / str(thread_id)
            ).resolve()
        )
