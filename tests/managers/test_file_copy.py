"""Independent, owner-scoped file snapshots with caller-owned metadata commits."""

from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import opendal
import pytest
from anyio import Path as AsyncPath
from arcanus.materia.sqlalchemy import AsyncSession
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.datastructures import Headers

from octomate.database import async_session
from octomate.managers.files import FileManager
from octomate.schemas.files import File, Jsonl
from tests.support.users import a_user


@pytest.fixture
def files(tmp_path: Path) -> FileManager:
    return FileManager(
        storage=opendal.AsyncOperator("fs", root=str(tmp_path / "contents"))
    )


@pytest.mark.parametrize("end", [None, 0, 3, 6])
@pytest.mark.parametrize("owned", [False, True])
async def test_copy_preserves_metadata_and_has_independent_content(
    in_memory_engine: AsyncEngine, files: FileManager, end: int | None, owned: bool
) -> None:
    owner_id = (await a_user()).id if owned else None
    source = await files.write(
        UploadFile(
            BytesIO(b"{}\n{}\n"),
            filename="rollout.jsonl",
            headers=Headers({"content-type": "application/jsonl"}),
        ),
        owner_id=owner_id,
    )
    storage = files.storage
    backend = Mock(wraps=storage)
    backend.copy = Mock(wraps=storage.copy)
    backend.read = Mock(wraps=storage.read)
    backend.write = Mock(wraps=storage.write)
    files.storage = backend
    operation = (
        files.copy(source.id, owner_id=owner_id)
        if end is None
        else files.partial_copy(source.id, end=end, owner_id=owner_id)
    )
    async with operation as copied:
        assert await storage.read(copied.key) == b"{}\n{}\n"[:end]
        with pytest.raises(FileNotFoundError):
            await files.get(copied.id, owner_id=owner_id)
        async with async_session() as session:
            session.add(copied)
            await session.commit()
    if end is None:
        backend.copy.assert_called_once_with(source.key, copied.key)
        backend.read.assert_not_called()
        backend.write.assert_not_called()
    else:
        backend.copy.assert_not_called()
        if end:
            backend.read.assert_called_once_with(source.key, size=end)
        else:
            backend.read.assert_not_called()
        backend.write.assert_called_once_with(
            copied.key, b"{}\n{}\n"[:end], if_not_exists=True
        )
    assert copied.id != source.id
    assert copied.id.version == 7
    assert copied.key != source.key
    assert copied.created_at >= source.created_at
    assert copied.owner_id == source.owner_id
    assert copied.name == source.name
    assert copied.media_type == source.media_type
    assert copied.size == (source.size if end is None else end)
    assert isinstance(await files.get(copied.id, owner_id=owner_id), Jsonl)
    await files.append(
        source.id, BytesIO(b"later\n"), offset=source.size, owner_id=owner_id
    )
    assert await files.read(copied.id, owner_id=owner_id) == b"{}\n{}\n"[:end]
    await files.delete(copied.id, owner_id=owner_id)
    assert await files.read(source.id, owner_id=owner_id) == b"{}\n{}\nlater\n"


@pytest.mark.parametrize("end", [None, 3])
async def test_copy_can_classify_validated_binary_content(
    in_memory_engine: AsyncEngine, files: FileManager, end: int | None
) -> None:
    source = await files.write(UploadFile(BytesIO(b"{}\n"), filename="rollout.jsonl"))
    operation = (
        files.copy(source.id, media_type="application/jsonl")
        if end is None
        else files.partial_copy(source.id, end=end, media_type="application/jsonl")
    )
    async with operation as copied:
        async with async_session() as session:
            session.add(copied)
            await session.commit()
    assert isinstance(await files.get(copied.id), Jsonl)
    assert (await files.get(source.id)).media_type == "application/octet-stream"


@pytest.mark.parametrize("end", [-1, 7])
async def test_copy_rejects_invalid_bounds_without_creating_content(
    in_memory_engine: AsyncEngine, files: FileManager, tmp_path: Path, end: int
) -> None:
    source = await files.write(UploadFile(BytesIO(b"source"), filename="note"))
    with pytest.raises(ValueError, match="Copy end"):
        async with files.partial_copy(source.id, end=end):
            pytest.fail("Invalid bounds must be rejected before yielding")
    assert [path.name async for path in AsyncPath(tmp_path / "contents").iterdir()] == [
        source.key
    ]


@pytest.mark.parametrize("failure", ["block", "commit"])
@pytest.mark.parametrize("end", [None, 3])
async def test_failed_copy_block_removes_only_the_copy(
    in_memory_engine: AsyncEngine,
    files: FileManager,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    end: int | None,
) -> None:
    source = await files.write(UploadFile(BytesIO(b"source"), filename="note"))
    if failure == "commit":
        monkeypatch.setattr(
            AsyncSession, "commit", AsyncMock(side_effect=RuntimeError("commit failed"))
        )
    operation = (
        files.copy(source.id) if end is None else files.partial_copy(source.id, end=end)
    )
    # The failure must exit both contexts to exercise caller-owned transaction cleanup.
    with pytest.raises(RuntimeError, match="failed"):  # noqa: PT012
        async with operation as copied, async_session() as session:
            session.add(copied)
            if failure == "block":
                raise RuntimeError("fork failed")
            await session.commit()
    assert not await files.storage.exists(copied.key)
    assert await files.read(source.id) == b"source"
    async with async_session() as session:
        assert await session.count(File) == 1


@pytest.mark.parametrize("end", [None, 3])
async def test_copy_refuses_an_existing_key(
    in_memory_engine: AsyncEngine,
    files: FileManager,
    monkeypatch: pytest.MonkeyPatch,
    end: int | None,
) -> None:
    source = await files.write(UploadFile(BytesIO(b"source"), filename="note"))
    monkeypatch.setattr("octomate.managers.files.uuid7", lambda: source.id)
    operation = (
        files.copy(source.id) if end is None else files.partial_copy(source.id, end=end)
    )
    with pytest.raises(FileExistsError):
        async with operation:
            pytest.fail("An existing key must not be overwritten")
    assert await files.read(source.id) == b"source"


@pytest.mark.parametrize("failure", ["provider", "size"])
async def test_direct_copy_failure_removes_partial_content(
    in_memory_engine: AsyncEngine,
    files: FileManager,
    tmp_path: Path,
    failure: str,
) -> None:
    source = await files.write(UploadFile(BytesIO(b"source"), filename="note"))
    storage = files.storage

    async def incomplete_copy(source_key: str, target_key: str) -> None:
        await storage.write(target_key, b"bad")
        if failure == "provider":
            raise RuntimeError("copy failed")

    backend = Mock(wraps=storage)
    backend.copy = AsyncMock(side_effect=incomplete_copy)
    files.storage = backend
    with pytest.raises(RuntimeError if failure == "provider" else ValueError):
        async with files.copy(source.id):
            pytest.fail("A failed copy must not yield metadata")
    assert [path.name async for path in AsyncPath(tmp_path / "contents").iterdir()] == [
        source.key
    ]
    assert await files.read(source.id) == b"source"
    async with async_session() as session:
        assert await session.count(File) == 1


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("end", [None, 3])
async def test_copy_refuses_missing_or_inconsistent_source_content(
    in_memory_engine: AsyncEngine,
    files: FileManager,
    tmp_path: Path,
    missing: bool,
    end: int | None,
) -> None:
    source = await files.write(UploadFile(BytesIO(b"source"), filename="note"))
    if missing:
        await files.storage.delete(source.key)
    else:
        await files.storage.write(source.key, b"uncommitted append", append=True)
    operation = (
        files.copy(source.id) if end is None else files.partial_copy(source.id, end=end)
    )
    with pytest.raises(FileNotFoundError if missing else ValueError):
        async with operation:
            pytest.fail("An inconsistent source must not be copied")
    assert [path.name async for path in AsyncPath(tmp_path / "contents").iterdir()] == (
        [] if missing else [source.key]
    )
