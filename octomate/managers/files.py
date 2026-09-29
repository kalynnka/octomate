"""Tracked files with pluggable content storage."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

import opendal
from anyio import to_thread
from fastapi import UploadFile
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers.base import Locks, Manager
from octomate.schemas.files import File, FileVariant, FileVariantAdapter
from octomate.types.files import FileProviderName


@dataclass
class FileManager(Manager, Locks[uuid.UUID]):
    """Keep file metadata in the database and contents in the injected OpenDAL operator.

    Every write gets a fresh ID and key; append extends an existing file. `get` returns
    metadata; `read` returns bytes. All lookups are scoped to the provider and owner.
    Callers supply the authenticated owner's ID, never an unverified client claim.
    Omitting the owner accesses only unowned service files, not every user's files.

    Storage and database writes are not one transaction. A failed metadata commit
    triggers content deletion; a failed content deletion retains metadata. Process
    interruption or a failed cleanup can leave an orphan, and a failed database
    commit after deletion can leave metadata for missing content. Retrying delete
    can finish that operation. No background reconciliation is performed.

    Append commits the new size after storage succeeds. A failed append or commit
    can leave extra bytes in storage; reads and appends reject a size mismatch.
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

    async def write(
        self, file: UploadFile, *, owner_id: uuid.UUID | None = None
    ) -> FileVariant:
        """Store complete content, then commit its validated metadata."""
        async with (
            self.upload(file, owner_id=owner_id) as stored,
            async_session() as session,
        ):
            session.add(stored)
            await session.commit()
        return stored

    @asynccontextmanager
    async def upload(
        self, file: UploadFile, *, owner_id: uuid.UUID | None = None
    ) -> AsyncGenerator[FileVariant]:
        """Upload content for metadata committed in the caller's transaction.

        The caller must add the yielded metadata and commit before leaving this
        context. If the block raises, delete the uploaded content.

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
                "owner_id": owner_id,
                "name": file.filename,
                "media_type": media_type.strip().lower(),
                "size": len(data),
                "provider": self.provider,
                "key": (
                    f"users/{owner_id.hex}/{file_id.hex}"
                    if owner_id is not None
                    else file_id.hex
                ),
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
            yield stored
        except BaseException:
            await self.storage.delete(stored.key)
            raise

    async def get(
        self, file_id: uuid.UUID, *, owner_id: uuid.UUID | None = None
    ) -> FileVariant:
        """Load metadata, or raise `FileNotFoundError` outside the owner's scope."""
        async with async_session() as session:
            stored = await session.one_or_none(
                File,
                expressions=[
                    File["id"] == file_id,
                    File["provider"] == self.provider,
                    File["owner_id"] == owner_id,
                ],
            )
        if stored is None:
            raise FileNotFoundError(str(file_id))
        return FileVariantAdapter.validate_python(stored)

    async def append(
        self,
        file_id: uuid.UUID,
        file: BinaryIO,
        *,
        offset: int,
        owner_id: uuid.UUID | None = None,
    ) -> FileVariant:
        """Append at the expected size, publishing metadata only after storage succeeds.

        The stream contains only new bytes; rewind it and leave it open at EOF.
        Keep the existing identity, filename, and MIME type. Storage changes cannot
        be undone if the commit fails.
        """
        if not self.storage.capability().write_can_append:
            raise ValueError("File storage does not support append")
        async with self.lock(file_id), async_session() as session:
            stored = await session.one_or_none(
                File,
                expressions=[
                    File["id"] == file_id,
                    File["provider"] == self.provider,
                    File["owner_id"] == owner_id,
                ],
            )
            if stored is None:
                raise FileNotFoundError(str(file_id))
            if stored.size != offset:
                raise ValueError("Append offset does not match the stored file size")
            metadata = await self.storage.stat(stored.key)
            if metadata.content_length != stored.size:
                raise ValueError("Stored content size differs from file metadata")
            await to_thread.run_sync(file.seek, 0)
            data = await to_thread.run_sync(file.read)
            if data:
                await self.storage.write(stored.key, data, append=True)
                stored.size += len(data)
                await session.commit()
            return FileVariantAdapter.validate_python(stored)

    async def read(
        self, file_id: uuid.UUID, *, owner_id: uuid.UUID | None = None
    ) -> bytes:
        """Read the content addressed by persisted metadata."""
        async with self.lock(file_id):
            stored = await self.get(file_id, owner_id=owner_id)
            try:
                data = await self.storage.read(stored.key)
            except opendal.exceptions.NotFound as exc:
                raise FileNotFoundError(stored.key) from exc
            if len(data) != stored.size:
                raise ValueError("Stored content size differs from file metadata")
            return data

    async def delete(
        self, file_id: uuid.UUID, *, owner_id: uuid.UUID | None = None
    ) -> None:
        """Delete content before removing its tracking record."""
        async with self.lock(file_id), async_session() as session:
            stored = await session.one_or_none(
                File,
                expressions=[
                    File["id"] == file_id,
                    File["provider"] == self.provider,
                    File["owner_id"] == owner_id,
                ],
            )
            if stored is None:
                raise FileNotFoundError(str(file_id))
            await self.storage.delete(stored.key)
            await session.delete(stored)
            await session.commit()
