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

    root: Path = Path(".octomate")  # Rows record paths beneath it, so it can move.
    directories: dict[Path, HostDirectory] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.root = self.root.resolve()

    @property
    def volumes_dir(self) -> Path:
        """Where registered owners' volumes are allocated."""
        return self.root / "volumes"

    @property
    def mirrors_dir(self) -> Path:
        """Where the service's project mirrors are kept."""
        return self.root / "mirrors"

    def get(self, path: Path) -> HostDirectory:
        """Reuse one lock and use count for every reference to this directory.

        A relative path is one a row recorded beneath the root.
        """
        path = self.root / path
        if path not in self.directories:
            self.directories[path] = HostDirectory(
                path, root=self.root if path.is_relative_to(self.root) else None
            )
        return self.directories[path]

    def temporary(self, thread_id: UUID7) -> HostDirectory:
        """An unregistered chat's stable temporary location, without a volume row."""
        return self.get(
            (
                Path(tempfile.gettempdir()) / "octomate-unregistered" / str(thread_id)
            ).resolve()
        )
