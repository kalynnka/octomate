"""Tracked files with pluggable content storage."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

import opendal
from fastapi import UploadFile
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers.base import Manager
from octomate.schemas.files import File, FileVariant, FileVariantAdapter
from octomate.types.files import FileProviderName


@dataclass
class FileManager(Manager):
    """Keep file metadata in the database and contents in the injected OpenDAL operator.

    Files are immutable: every write gets a fresh ID and key. `get` returns
    metadata; `read` returns bytes. All lookups are scoped to the provider name.
    Callers own access control; an ID alone is not authorization to serve a file.

    Storage and database writes are not one transaction. A failed metadata commit
    triggers content deletion; a failed content deletion retains metadata. Process
    interruption or a failed cleanup can leave an orphan, and a failed database
    commit after deletion can leave metadata for missing content. Retrying delete
    can finish that operation. No background reconciliation is performed.
    """

    provider: FileProviderName = "filesystem"
    storage: opendal.AsyncOperator = field(
        default_factory=lambda: opendal.AsyncOperator(
            "fs", root=str(Path(".octomate/files").absolute())
        )
    )

    def __post_init__(self) -> None:
        if not self.storage.capability().write_with_if_not_exists:
            raise ValueError("File storage must support writes without overwriting")

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
                "provider": self.provider,
                "key": file_id.hex,
            }
        )
        try:
            await self.storage.write(stored.key, data, if_not_exists=True)
        except (
            opendal.exceptions.AlreadyExists,
            opendal.exceptions.ConditionNotMatch,
        ) as exc:
            raise FileExistsError(stored.key) from exc
        try:
            async with async_session() as session:
                session.add(stored)
                await session.commit()
        except Exception:
            await self.storage.delete(stored.key)
            raise
        return stored

    async def get(self, file_id: uuid.UUID) -> FileVariant:
        """Load metadata, or raise `FileNotFoundError` for an unknown file."""
        async with async_session() as session:
            stored = await session.one_or_none(
                File,
                expressions=[
                    File["id"] == file_id,
                    File["provider"] == self.provider,
                ],
            )
        if stored is None:
            raise FileNotFoundError(str(file_id))
        return FileVariantAdapter.validate_python(stored)

    async def read(self, file_id: uuid.UUID) -> bytes:
        """Read the content addressed by persisted metadata."""
        stored = await self.get(file_id)
        try:
            return await self.storage.read(stored.key)
        except opendal.exceptions.NotFound as exc:
            raise FileNotFoundError(stored.key) from exc

    async def delete(self, file_id: uuid.UUID) -> None:
        """Delete content before removing its tracking record."""
        async with async_session() as session:
            stored = await session.one_or_none(
                File,
                expressions=[
                    File["id"] == file_id,
                    File["provider"] == self.provider,
                ],
            )
            if stored is None:
                raise FileNotFoundError(str(file_id))
            await self.storage.delete(stored.key)
            await session.delete(stored)
            await session.commit()
