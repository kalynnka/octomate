"""Tracked file lifecycle over interchangeable content providers."""

from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from tempfile import SpooledTemporaryFile
from unittest.mock import AsyncMock

import pytest
from arcanus.materia.sqlalchemy import AsyncSession
from fastapi import UploadFile
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.datastructures import Headers
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers.files import FileManager, FileProvider, FilesystemProvider
from octomate.schemas.files import (
    Binary,
    File,
    Gif,
    Image,
    Jpeg,
    Json,
    Jsonl,
    Markdown,
    Png,
    Text,
)
from octomate.types.files import FileProviderName


@dataclass
class MemoryFileProvider:
    """An in-memory stand-in for the S3 provider contract."""

    name: FileProviderName = "s3"
    contents: dict[str, bytes] = field(default_factory=dict)

    async def write(self, key: str, data: bytes) -> None:
        if key in self.contents:
            raise FileExistsError(key)
        self.contents[key] = data

    async def read(self, key: str) -> bytes:
        if key not in self.contents:
            raise FileNotFoundError(key)
        return self.contents[key]

    async def delete(self, key: str) -> None:
        self.contents.pop(key, None)


@pytest.fixture(params=["filesystem", "memory"])
def provider(request: pytest.FixtureRequest, tmp_path: Path) -> FileProvider:
    if request.param == "filesystem":
        return FilesystemProvider(root=tmp_path / "contents")
    return MemoryFileProvider()


@pytest.mark.parametrize("data", [b"", bytes(range(256)), "你好".encode()])
async def test_round_trip_and_metadata(
    in_memory_engine: AsyncEngine, provider: FileProvider, data: bytes
) -> None:
    files = FileManager(provider)
    stored = await files.write(
        UploadFile(
            BytesIO(data),
            filename="transcript.jsonl",
            headers=Headers({"content-type": "application/jsonl"}),
        )
    )

    reloaded = await FileManager(provider).get(stored.id)
    assert reloaded.model_dump() == stored.model_dump()
    assert reloaded.size == len(data)
    assert reloaded.media_type == "application/jsonl"
    assert reloaded.name == "transcript.jsonl"
    assert reloaded.provider == provider.name
    assert reloaded.created_at.utcoffset() is not None
    assert await FileManager(provider).read(stored.id) == data


async def test_repeated_filenames_have_independent_storage(
    in_memory_engine: AsyncEngine, provider: FileProvider
) -> None:
    files = FileManager(provider)
    first = await files.write(UploadFile(BytesIO(b"first"), filename="note.txt"))
    second = await files.write(UploadFile(BytesIO(b"second"), filename="note.txt"))

    assert first.id != second.id
    assert first.key != second.key
    assert first.media_type == "application/octet-stream"
    assert await files.read(first.id) == b"first"
    assert await files.read(second.id) == b"second"


@pytest.mark.parametrize("max_size", [1, 1024])
async def test_upload_metadata_and_complete_content(
    in_memory_engine: AsyncEngine, provider: FileProvider, max_size: int
) -> None:
    upload = UploadFile(
        # Pyright is wrong about runtime: UploadFile explicitly supports spooled files.
        SpooledTemporaryFile(max_size=max_size),  # pyright: ignore[reportArgumentType]
        filename="notes.md",
        headers=Headers({"content-type": "Text/Markdown; charset=utf-8"}),
        size=999,
    )
    try:
        await upload.write(b"# Complete contents")
        await upload.seek(5)
        files = FileManager(provider)
        stored = await files.write(upload)

        assert isinstance(stored, Markdown)
        assert stored.name == "notes.md"
        assert stored.size == len(b"# Complete contents")
        assert await files.read(stored.id) == b"# Complete contents"
        assert not upload.file.closed
        assert await upload.read() == b""
    finally:
        await upload.close()


async def test_missing_upload_filename_is_rejected_before_storage(
    in_memory_engine: AsyncEngine,
    provider: FileProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write = AsyncMock()
    monkeypatch.setattr(provider, "write", write)
    with pytest.raises(ValidationError, match="name"):
        await FileManager(provider).write(UploadFile(BytesIO(b"contents")))
    write.assert_not_awaited()


async def test_delete_removes_content_and_metadata(
    in_memory_engine: AsyncEngine, provider: FileProvider
) -> None:
    files = FileManager(provider)
    stored = await files.write(UploadFile(BytesIO(b"contents"), filename="note"))
    await files.delete(stored.id)

    with pytest.raises(FileNotFoundError):
        await files.get(stored.id)
    with pytest.raises(FileNotFoundError):
        await provider.read(stored.key)


async def test_missing_file(
    in_memory_engine: AsyncEngine, provider: FileProvider
) -> None:
    files = FileManager(provider)
    file_id = uuid7()
    with pytest.raises(FileNotFoundError):
        await files.get(file_id)
    with pytest.raises(FileNotFoundError):
        await files.read(file_id)
    with pytest.raises(FileNotFoundError):
        await files.delete(file_id)


async def test_other_provider_cannot_read_or_delete(
    in_memory_engine: AsyncEngine, provider: FileProvider
) -> None:
    files = FileManager(provider)
    stored = await files.write(UploadFile(BytesIO(b"contents"), filename="note"))
    other = FileManager(
        MemoryFileProvider(name="s3" if provider.name == "filesystem" else "filesystem")
    )

    with pytest.raises(FileNotFoundError):
        await other.read(stored.id)
    with pytest.raises(FileNotFoundError):
        await other.delete(stored.id)
    assert await files.read(stored.id) == b"contents"


async def test_unknown_provider_is_rejected_before_storage(
    in_memory_engine: AsyncEngine,
    provider: FileProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write = AsyncMock()
    monkeypatch.setattr(provider, "name", "unknown")
    monkeypatch.setattr(provider, "write", write)
    with pytest.raises(ValidationError, match="literal_error"):
        await FileManager(provider).write(
            UploadFile(BytesIO(b"contents"), filename="note")
        )
    write.assert_not_awaited()


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "../note.txt",
        "folder/note.txt",
        "/note.txt",
        "note.txt/",
        "C:\\note.txt",
        "folder\\note.txt",
        "C:note.txt",
        "note\x00.txt",
    ],
)
async def test_filename_validation_precedes_storage(
    in_memory_engine: AsyncEngine,
    provider: FileProvider,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    write = AsyncMock()
    monkeypatch.setattr(provider, "write", write)
    with pytest.raises(ValidationError, match="filename"):
        await FileManager(provider).write(
            UploadFile(BytesIO(b"contents"), filename=name)
        )
    write.assert_not_awaited()


@pytest.mark.parametrize(
    ("name", "media_type", "expected"),
    [
        ("data.bin", "application/octet-stream", Binary),
        ("notes.txt", "text/plain", Text),
        ("README.md", "text/markdown", Markdown),
        ("config.json", "application/json", Json),
        ("session.jsonl", "application/jsonl", Jsonl),
        ("animation.gif", "image/gif", Gif),
        ("screenshot.png", "image/png", Png),
        ("photo.jpg", "image/jpeg", Jpeg),
        ("photo.jpeg", "image/jpeg", Jpeg),
    ],
)
async def test_mime_type_restores_concrete_subtype(
    in_memory_engine: AsyncEngine,
    provider: FileProvider,
    name: str,
    media_type: str,
    expected: type[File],
) -> None:
    files = FileManager(provider)
    stored = await files.write(
        UploadFile(
            BytesIO(b"contents"),
            filename=name,
            headers=Headers({"content-type": media_type}),
        )
    )
    assert type(stored) is expected
    assert type(await FileManager(provider).get(stored.id)) is expected
    async with async_session() as session:
        rows = await session.list(File)
    assert len(rows) == 1
    assert type(rows[0]) is expected


async def test_image_parent_query_selects_only_image_subtypes(
    in_memory_engine: AsyncEngine, provider: FileProvider
) -> None:
    files = FileManager(provider)
    await files.write(
        UploadFile(
            BytesIO(b"# Title"),
            filename="README.md",
            headers=Headers({"content-type": "text/markdown"}),
        )
    )
    gif = await files.write(
        UploadFile(
            BytesIO(b"gif"),
            filename="animation.gif",
            headers=Headers({"content-type": "image/gif"}),
        )
    )
    png = await files.write(
        UploadFile(
            BytesIO(b"png"),
            filename="screenshot.png",
            headers=Headers({"content-type": "image/png"}),
        )
    )
    jpeg = await files.write(
        UploadFile(
            BytesIO(b"jpeg"),
            filename="photo.jpg",
            headers=Headers({"content-type": "image/jpeg"}),
        )
    )

    async with async_session() as session:
        images = await session.list(Image)
    assert {image.id for image in images} == {gif.id, png.id, jpeg.id}
    assert {type(image) for image in images} == {Gif, Png, Jpeg}


@pytest.mark.parametrize(
    "media_type", ["application/pdf", "image/webp", "application/x-custom"]
)
async def test_unrecognized_mime_type_round_trips_as_base_file(
    in_memory_engine: AsyncEngine,
    provider: FileProvider,
    media_type: str,
) -> None:
    files = FileManager(provider)
    stored = await files.write(
        UploadFile(
            BytesIO(b"contents"),
            filename="data.custom",
            headers=Headers({"content-type": media_type}),
        )
    )
    assert type(stored) is File
    assert stored.media_type == media_type
    restored = await FileManager(provider).get(stored.id)
    assert type(restored) is File
    assert restored.model_dump() == stored.model_dump()
    assert await files.read(stored.id) == b"contents"
    async with async_session() as session:
        rows = await session.list(File)
        assert len(rows) == 1
        assert type(rows[0]) is File
        assert rows[0].media_type == media_type
        assert not await session.list(Image)
    await files.delete(stored.id)
    with pytest.raises(FileNotFoundError):
        await files.get(stored.id)


async def test_filename_is_preserved(
    in_memory_engine: AsyncEngine, provider: FileProvider
) -> None:
    stored = await FileManager(provider).write(
        UploadFile(
            BytesIO(b"# Notes"),
            filename="项目 notes.md",
            headers=Headers({"content-type": "text/markdown"}),
        )
    )
    assert (await FileManager(provider).get(stored.id)).name == "项目 notes.md"


async def test_failed_content_write_publishes_no_metadata(
    in_memory_engine: AsyncEngine,
    provider: FileProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provider, "write", AsyncMock(side_effect=OSError("storage full"))
    )
    with pytest.raises(OSError, match="storage full"):
        await FileManager(provider).write(
            UploadFile(BytesIO(b"contents"), filename="note")
        )
    async with async_session() as session:
        assert not await session.list(File)


async def test_failed_metadata_commit_removes_content(
    in_memory_engine: AsyncEngine,
    provider: FileProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write = AsyncMock(wraps=provider.write)
    monkeypatch.setattr(provider, "write", write)
    with monkeypatch.context() as patch:
        patch.setattr(
            AsyncSession, "commit", AsyncMock(side_effect=RuntimeError("commit failed"))
        )
        with pytest.raises(RuntimeError, match="commit failed"):
            await FileManager(provider).write(
                UploadFile(BytesIO(b"contents"), filename="note")
            )

    key = write.call_args.args[0]
    with pytest.raises(FileNotFoundError):
        await provider.read(key)
    async with async_session() as session:
        assert not await session.list(File)


async def test_failed_content_delete_retains_metadata(
    in_memory_engine: AsyncEngine,
    provider: FileProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = FileManager(provider)
    stored = await files.write(UploadFile(BytesIO(b"contents"), filename="note"))
    monkeypatch.setattr(
        provider, "delete", AsyncMock(side_effect=OSError("unavailable"))
    )
    with pytest.raises(OSError, match="unavailable"):
        await files.delete(stored.id)
    assert (await files.get(stored.id)).key == stored.key
    assert await files.read(stored.id) == b"contents"


async def test_delete_can_finish_after_content_is_already_gone(
    in_memory_engine: AsyncEngine, provider: FileProvider
) -> None:
    files = FileManager(provider)
    stored = await files.write(UploadFile(BytesIO(b"contents"), filename="note"))
    await provider.delete(stored.key)
    await files.delete(stored.id)
    with pytest.raises(FileNotFoundError):
        await files.get(stored.id)


async def test_provider_refuses_overwrite(provider: FileProvider) -> None:
    key = uuid7().hex
    await provider.write(key, b"original")
    with pytest.raises(FileExistsError):
        await provider.write(key, b"replacement")
    assert await provider.read(key) == b"original"


@pytest.mark.parametrize("key", ["../outside", "/tmp/outside", "", "a/b", ".."])
async def test_filesystem_refuses_invalid_keys(tmp_path: Path, key: str) -> None:
    provider = FilesystemProvider(root=tmp_path / "contents")
    with pytest.raises(ValueError, match="UUID"):
        await provider.write(key, b"contents")
    with pytest.raises(ValueError, match="UUID"):
        await provider.read(key)
    with pytest.raises(ValueError, match="UUID"):
        await provider.delete(key)


async def test_filesystem_survives_new_provider_instance(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> None:
    root = tmp_path / "contents"
    original = FileManager(FilesystemProvider(root=root))
    stored = await original.write(UploadFile(BytesIO(b"persistent"), filename="note"))
    restored = FileManager(FilesystemProvider(root=root))
    assert await restored.read(stored.id) == b"persistent"
