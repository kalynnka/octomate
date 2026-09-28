"""Tracked files with pluggable content storage."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from anyio import Path as AsyncPath
from fastapi import UploadFile
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers.base import Manager
from octomate.schemas.files import File, FileVariant, FileVariantAdapter
from octomate.types.files import FileProviderName


class FileProvider(Protocol):
    """Content storage independent of metadata and local filesystem paths.

    Keys are generated UUID hex strings. Writes store complete bytes and must
    refuse an existing key. Reads of missing keys raise `FileNotFoundError`;
    deletes succeed when a key is already absent. Implementations own their I/O
    resources and must not block the event loop.
    """

    name: FileProviderName  # Stable deployment identity tied to one storage location.

    async def write(self, key: str, data: bytes) -> None: ...

    async def read(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...


@dataclass
class FilesystemProvider:
    """Store contents under a private directory, using keys rather than filenames.

    The directory is created on the first write. A deployment must give each
    storage location its own stable `name`; changing the root does not move files.
    """

    root: Path = Path(".octomate/files")
    name: FileProviderName = "filesystem"

    def __post_init__(self) -> None:
        self.root = self.root.expanduser().absolute()

    def path(self, key: str) -> AsyncPath:
        """Accept only the flat UUID keys minted by the manager."""
        if uuid.UUID(key).hex != key:
            raise ValueError("File keys must be UUID hex strings")
        return AsyncPath(self.root / key)

    async def write(self, key: str, data: bytes) -> None:
        path = self.path(key)
        await path.parent.mkdir(parents=True, exist_ok=True)
        async with await path.open("xb") as stream:
            await stream.write(data)

    async def read(self, key: str) -> bytes:
        return await self.path(key).read_bytes()

    async def delete(self, key: str) -> None:
        await self.path(key).unlink(missing_ok=True)


@dataclass
class FileManager(Manager):
    """Keep file metadata in the database and contents in the injected provider.

    Files are immutable: every write gets a fresh ID and key. `get` returns
    metadata; `read` returns bytes. All lookups are scoped to the provider's name.
    Callers own access control; an ID alone is not authorization to serve a file.

    Storage and database writes are not one transaction. A failed metadata commit
    triggers content deletion; a failed content deletion retains metadata. Process
    interruption or a failed cleanup can leave an orphan, and a failed database
    commit after deletion can leave metadata for missing content. Retrying delete
    can finish that operation. No background reconciliation is performed.
    """

    provider: FileProvider = field(default_factory=FilesystemProvider)

    async def write(self, file: UploadFile) -> FileVariant:
        """Store complete content, then publish its validated metadata.

        Filename and MIME type come from the upload; size is measured from its
        complete contents. A missing content type means generic binary content.
        The filename must have no directories. Unrecognized MIME types use the
        base File metadata; content is not inspected to infer its format. The upload
        is rewound before reading and left open at EOF for its caller to close.
        """
        await file.seek(0)
        data = await file.read()
        media_type = (file.content_type or "application/octet-stream").split(";", 1)[0]
        file_id = uuid7()
        stored = FileVariantAdapter.validate_python(
            {
                "id": file_id,
                "name": file.filename,
                "media_type": media_type.strip().lower(),
                "size": len(data),
                "provider": self.provider.name,
                "key": file_id.hex,
            }
        )
        await self.provider.write(stored.key, data)
        try:
            async with async_session() as session:
                session.add(stored)
                await session.commit()
        except Exception:
            await self.provider.delete(stored.key)
            raise
        return stored

    async def get(self, file_id: uuid.UUID) -> FileVariant:
        """Load metadata, or raise `FileNotFoundError` for an unknown file."""
        async with async_session() as session:
            stored = await session.one_or_none(
                File,
                expressions=[
                    File["id"] == file_id,
                    File["provider"] == self.provider.name,
                ],
            )
        if stored is None:
            raise FileNotFoundError(str(file_id))
        return FileVariantAdapter.validate_python(stored)

    async def read(self, file_id: uuid.UUID) -> bytes:
        """Read the content addressed by persisted metadata."""
        stored = await self.get(file_id)
        return await self.provider.read(stored.key)

    async def delete(self, file_id: uuid.UUID) -> None:
        """Delete content before removing its tracking record."""
        async with async_session() as session:
            stored = await session.one_or_none(
                File,
                expressions=[
                    File["id"] == file_id,
                    File["provider"] == self.provider.name,
                ],
            )
            if stored is None:
                raise FileNotFoundError(str(file_id))
            await self.provider.delete(stored.key)
            await session.delete(stored)
            await session.commit()
