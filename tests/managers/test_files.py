"""Tracked file lifecycle over OpenDAL filesystem storage."""

from io import BytesIO
from pathlib import Path
from tempfile import SpooledTemporaryFile, TemporaryFile
from unittest.mock import AsyncMock, Mock

import opendal
import pytest
from anyio import Path as AsyncPath
from arcanus.materia.sqlalchemy import AsyncSession
from fastapi import UploadFile
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.datastructures import Headers
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers.files import FileManager
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
from tests.support.users import a_user


@pytest.fixture
def storage(tmp_path: Path) -> opendal.AsyncOperator:
    return opendal.AsyncOperator("fs", root=str(tmp_path / "contents"))


@pytest.mark.parametrize("data", [b"", bytes(range(256)), "你好".encode()])
async def test_round_trip_and_metadata(
    in_memory_engine: AsyncEngine, storage: opendal.AsyncOperator, data: bytes
) -> None:
    files = FileManager(storage=storage)
    stored = await files.write(
        UploadFile(
            BytesIO(data),
            filename="transcript.jsonl",
            headers=Headers({"content-type": "application/jsonl"}),
        )
    )

    reloaded = await FileManager(storage=storage).get(stored.id)
    assert reloaded.model_dump() == stored.model_dump()
    assert reloaded.size == len(data)
    assert reloaded.media_type == "application/jsonl"
    assert reloaded.name == "transcript.jsonl"
    assert reloaded.provider == "filesystem"
    assert reloaded.created_at.utcoffset() is not None
    assert await FileManager(storage=storage).read(stored.id) == data


@pytest.mark.parametrize("delta", [b"", b"second\n"])
@pytest.mark.parametrize("on_disk", [False, True])
async def test_append_preserves_identity_and_updates_persisted_size(
    in_memory_engine: AsyncEngine,
    storage: opendal.AsyncOperator,
    delta: bytes,
    on_disk: bool,
) -> None:
    files = FileManager(storage=storage)
    original = await files.write(UploadFile(BytesIO(b"first\n"), filename="note"))
    mocked_storage = Mock(wraps=storage)
    mocked_storage.write = AsyncMock(wraps=storage.write)
    files.storage = mocked_storage

    with TemporaryFile() if on_disk else BytesIO() as stream:
        stream.write(delta)
        updated = await files.append(original.id, stream, offset=original.size)
        assert not stream.closed
        assert stream.read() == b""

    assert updated.id == original.id
    assert updated.key == original.key
    assert updated.name == original.name
    assert updated.media_type == original.media_type
    assert updated.size == original.size + len(delta)
    assert original.size == len(b"first\n")
    assert (await files.get(original.id)).size == updated.size
    assert await files.read(original.id) == b"first\n" + delta
    if delta:
        mocked_storage.write.assert_awaited_once_with(original.key, delta, append=True)
    else:
        mocked_storage.write.assert_not_awaited()


async def test_append_refuses_a_stale_offset(
    in_memory_engine: AsyncEngine, storage: opendal.AsyncOperator
) -> None:
    files = FileManager(storage=storage)
    original = await files.write(UploadFile(BytesIO(b"first\n"), filename="note"))
    await files.append(original.id, BytesIO(b"second\n"), offset=original.size)

    with pytest.raises(ValueError, match="Append offset"):
        await files.append(original.id, BytesIO(b"again\n"), offset=original.size)

    assert await files.read(original.id) == b"first\nsecond\n"


async def test_append_is_scoped_to_the_owner(
    in_memory_engine: AsyncEngine, storage: opendal.AsyncOperator
) -> None:
    files = FileManager(storage=storage)
    owner = await a_user()
    other = await a_user("other")
    original = await files.write(
        UploadFile(BytesIO(b"private\n"), filename="note"), owner_id=owner.id
    )

    with pytest.raises(FileNotFoundError):
        await files.append(
            original.id,
            BytesIO(b"foreign\n"),
            offset=original.size,
            owner_id=other.id,
        )

    assert await files.read(original.id, owner_id=owner.id) == b"private\n"


@pytest.mark.parametrize("failure", ["storage", "commit"])
async def test_failed_append_leaves_metadata_unchanged(
    in_memory_engine: AsyncEngine,
    storage: opendal.AsyncOperator,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    files = FileManager(storage=storage)
    original = await files.write(UploadFile(BytesIO(b"first\n"), filename="note"))
    with monkeypatch.context() as patch:
        if failure == "storage":
            mocked_storage = Mock(wraps=storage)
            mocked_storage.write = AsyncMock(side_effect=RuntimeError("append failed"))
            patch.setattr(files, "storage", mocked_storage)
        else:
            patch.setattr(
                AsyncSession,
                "commit",
                AsyncMock(side_effect=RuntimeError("commit failed")),
            )
        with pytest.raises(RuntimeError, match="failed"):
            await files.append(original.id, BytesIO(b"second\n"), offset=original.size)

    assert (await files.get(original.id)).size == original.size
    if failure == "storage":
        assert await files.read(original.id) == b"first\n"
    else:
        assert await storage.read(original.key) == b"first\nsecond\n"
        with pytest.raises(ValueError, match="differs from file metadata"):
            await files.read(original.id)
        with pytest.raises(ValueError, match="differs from file metadata"):
            await files.append(original.id, BytesIO(b"retry\n"), offset=original.size)


async def test_append_refuses_unsupported_storage(
    in_memory_engine: AsyncEngine, storage: opendal.AsyncOperator
) -> None:
    files = FileManager(storage=storage)
    original = await files.write(UploadFile(BytesIO(b"first\n"), filename="note"))
    mocked_storage = Mock(wraps=storage)
    mocked_storage.capability = Mock(return_value=Mock(write_can_append=False))
    mocked_storage.write = AsyncMock()
    files.storage = mocked_storage

    with pytest.raises(ValueError, match="does not support append"):
        await files.append(original.id, BytesIO(b"second\n"), offset=original.size)

    mocked_storage.write.assert_not_awaited()


async def test_repeated_filenames_have_independent_storage(
    in_memory_engine: AsyncEngine, storage: opendal.AsyncOperator
) -> None:
    files = FileManager(storage=storage)
    first = await files.write(UploadFile(BytesIO(b"first"), filename="note.txt"))
    second = await files.write(UploadFile(BytesIO(b"second"), filename="note.txt"))

    assert first.id != second.id
    assert first.key != second.key
    assert first.media_type == "application/octet-stream"
    assert await files.read(first.id) == b"first"
    assert await files.read(second.id) == b"second"


@pytest.mark.parametrize("max_size", [1, 1024])
async def test_upload_metadata_and_complete_content(
    in_memory_engine: AsyncEngine, storage: opendal.AsyncOperator, max_size: int
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
        files = FileManager(storage=storage)
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
    storage: opendal.AsyncOperator,
) -> None:
    write = AsyncMock()
    mocked_storage = Mock(wraps=storage)
    mocked_storage.write = write
    with pytest.raises(ValidationError, match="name"):
        await FileManager(storage=mocked_storage).write(
            UploadFile(BytesIO(b"contents"))
        )
    write.assert_not_awaited()


async def test_delete_removes_content_and_metadata(
    in_memory_engine: AsyncEngine, storage: opendal.AsyncOperator
) -> None:
    files = FileManager(storage=storage)
    stored = await files.write(UploadFile(BytesIO(b"contents"), filename="note"))
    await files.delete(stored.id)

    with pytest.raises(FileNotFoundError):
        await files.get(stored.id)
    with pytest.raises(opendal.exceptions.NotFound):
        await storage.read(stored.key)


async def test_missing_file(
    in_memory_engine: AsyncEngine, storage: opendal.AsyncOperator
) -> None:
    files = FileManager(storage=storage)
    file_id = uuid7()
    with pytest.raises(FileNotFoundError):
        await files.get(file_id)
    with pytest.raises(FileNotFoundError):
        await files.read(file_id)
    with pytest.raises(FileNotFoundError):
        await files.delete(file_id)


async def test_other_provider_cannot_read_or_delete(
    in_memory_engine: AsyncEngine, storage: opendal.AsyncOperator
) -> None:
    files = FileManager(storage=storage)
    stored = await files.write(UploadFile(BytesIO(b"contents"), filename="note"))
    async with async_session() as session:
        row = await session.one(File, expressions=[File["id"] == stored.id])
        row.provider = "s3"
        await session.commit()

    with pytest.raises(FileNotFoundError):
        await files.read(stored.id)
    with pytest.raises(FileNotFoundError):
        await files.delete(stored.id)
    assert await storage.read(stored.key) == b"contents"


async def test_unknown_provider_is_rejected_before_storage(
    in_memory_engine: AsyncEngine,
    storage: opendal.AsyncOperator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mocked_storage = Mock(wraps=storage)
    mocked_storage.write = AsyncMock()
    files = FileManager(storage=mocked_storage)
    monkeypatch.setattr(files, "provider", "unknown")
    with pytest.raises(ValidationError, match="literal_error"):
        await files.write(UploadFile(BytesIO(b"contents"), filename="note"))
    mocked_storage.write.assert_not_awaited()


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
    storage: opendal.AsyncOperator,
    name: str,
) -> None:
    write = AsyncMock()
    mocked_storage = Mock(wraps=storage)
    mocked_storage.write = write
    with pytest.raises(ValidationError, match="filename"):
        await FileManager(storage=mocked_storage).write(
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
    storage: opendal.AsyncOperator,
    name: str,
    media_type: str,
    expected: type[File],
) -> None:
    files = FileManager(storage=storage)
    stored = await files.write(
        UploadFile(
            BytesIO(b"contents"),
            filename=name,
            headers=Headers({"content-type": media_type}),
        )
    )
    assert type(stored) is expected
    assert type(await FileManager(storage=storage).get(stored.id)) is expected
    async with async_session() as session:
        rows = await session.list(File)
    assert len(rows) == 1
    assert type(rows[0]) is expected


async def test_image_parent_query_selects_only_image_subtypes(
    in_memory_engine: AsyncEngine, storage: opendal.AsyncOperator
) -> None:
    files = FileManager(storage=storage)
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
    storage: opendal.AsyncOperator,
    media_type: str,
) -> None:
    files = FileManager(storage=storage)
    stored = await files.write(
        UploadFile(
            BytesIO(b"contents"),
            filename="data.custom",
            headers=Headers({"content-type": media_type}),
        )
    )
    assert type(stored) is File
    assert stored.media_type == media_type
    restored = await FileManager(storage=storage).get(stored.id)
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
    in_memory_engine: AsyncEngine, storage: opendal.AsyncOperator
) -> None:
    stored = await FileManager(storage=storage).write(
        UploadFile(
            BytesIO(b"# Notes"),
            filename="项目 notes.md",
            headers=Headers({"content-type": "text/markdown"}),
        )
    )
    assert (await FileManager(storage=storage).get(stored.id)).name == "项目 notes.md"


async def test_failed_content_write_publishes_no_metadata(
    in_memory_engine: AsyncEngine,
    storage: opendal.AsyncOperator,
) -> None:
    mocked_storage = Mock(wraps=storage)
    mocked_storage.write = AsyncMock(side_effect=OSError("storage full"))
    with pytest.raises(OSError, match="storage full"):
        await FileManager(storage=mocked_storage).write(
            UploadFile(BytesIO(b"contents"), filename="note")
        )
    async with async_session() as session:
        assert not await session.list(File)


async def test_failed_metadata_commit_removes_content(
    in_memory_engine: AsyncEngine,
    storage: opendal.AsyncOperator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_id = uuid7()
    monkeypatch.setattr("octomate.managers.files.uuid7", lambda: file_id)
    with monkeypatch.context() as patch:
        patch.setattr(
            AsyncSession, "commit", AsyncMock(side_effect=RuntimeError("commit failed"))
        )
        with pytest.raises(RuntimeError, match="commit failed"):
            await FileManager(storage=storage).write(
                UploadFile(BytesIO(b"contents"), filename="note")
            )

    with pytest.raises(opendal.exceptions.NotFound):
        await storage.read(file_id.hex)
    async with async_session() as session:
        assert not await session.list(File)


async def test_failed_content_delete_retains_metadata(
    in_memory_engine: AsyncEngine,
    storage: opendal.AsyncOperator,
) -> None:
    files = FileManager(storage=storage)
    stored = await files.write(UploadFile(BytesIO(b"contents"), filename="note"))
    mocked_storage = Mock(wraps=storage)
    mocked_storage.delete = AsyncMock(side_effect=OSError("unavailable"))
    files.storage = mocked_storage
    with pytest.raises(OSError, match="unavailable"):
        await files.delete(stored.id)
    assert (await files.get(stored.id)).key == stored.key
    assert await files.read(stored.id) == b"contents"


async def test_delete_can_finish_after_content_is_already_gone(
    in_memory_engine: AsyncEngine, storage: opendal.AsyncOperator
) -> None:
    files = FileManager(storage=storage)
    stored = await files.write(UploadFile(BytesIO(b"contents"), filename="note"))
    await storage.delete(stored.key)
    await files.delete(stored.id)
    with pytest.raises(FileNotFoundError):
        await files.get(stored.id)


async def test_write_refuses_existing_key(
    in_memory_engine: AsyncEngine,
    storage: opendal.AsyncOperator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_id = uuid7()
    await storage.write(file_id.hex, b"original")
    monkeypatch.setattr("octomate.managers.files.uuid7", lambda: file_id)
    with pytest.raises(FileExistsError):
        await FileManager(storage=storage).write(
            UploadFile(BytesIO(b"replacement"), filename="note")
        )
    assert await storage.read(file_id.hex) == b"original"
    async with async_session() as session:
        assert not await session.list(File)


def test_storage_without_conditional_writes_is_rejected() -> None:
    storage = Mock(spec=opendal.AsyncOperator)
    storage.capability.return_value.write_with_if_not_exists = False
    with pytest.raises(ValueError, match="without overwriting"):
        FileManager(storage=storage)


async def test_missing_content_raises_file_not_found(
    in_memory_engine: AsyncEngine, storage: opendal.AsyncOperator
) -> None:
    files = FileManager(storage=storage)
    stored = await files.write(UploadFile(BytesIO(b"contents"), filename="note"))
    await storage.delete(stored.key)
    with pytest.raises(FileNotFoundError):
        await files.read(stored.id)


async def test_filesystem_survives_new_operator(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> None:
    root = str(tmp_path / "contents")
    original = FileManager(storage=opendal.AsyncOperator("fs", root=root))
    stored = await original.write(UploadFile(BytesIO(b"persistent"), filename="note"))
    restored = FileManager(storage=opendal.AsyncOperator("fs", root=root))
    assert await restored.read(stored.id) == b"persistent"


async def test_default_filesystem_root(
    in_memory_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    files = FileManager()
    stored = await files.write(UploadFile(BytesIO(b"contents"), filename="note"))
    root = tmp_path / ".octomate" / "files"
    assert await AsyncPath(root).is_dir()
    assert await AsyncPath(root / stored.key).read_bytes() == b"contents"
