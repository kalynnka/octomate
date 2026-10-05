"""File identity alone never grants access outside the authenticated owner's scope."""

from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import opendal
import pytest
from fastapi import UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.datastructures import Headers
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers.files import FileManager
from octomate.schemas.files import Jsonl
from octomate.schemas.user import User


@pytest.fixture
async def owners(in_memory_engine: AsyncEngine) -> tuple[User, User]:
    alice, bob = User(username="alice"), User(username="bob")
    async with async_session() as session:
        session.add_all([alice, bob])
        await session.commit()
    return alice, bob


@pytest.fixture
def storage(tmp_path: Path) -> opendal.AsyncOperator:
    return opendal.AsyncOperator("fs", root=str(tmp_path / "contents"))


async def test_owned_transcript_round_trip(
    owners: tuple[User, User], storage: opendal.AsyncOperator
) -> None:
    alice, bob = owners
    files = FileManager(storage=storage)
    stored = await files.write(
        UploadFile(
            BytesIO(b'{"session":"native"}\n'),
            filename="rollout.jsonl",
            headers=Headers({"content-type": "application/jsonl"}),
        ),
        owner_id=alice.id,
    )
    other = await files.write(
        UploadFile(BytesIO(b"another transcript"), filename="rollout.jsonl"),
        owner_id=bob.id,
    )

    reloaded = await FileManager(storage=storage).get(stored.id, owner_id=alice.id)
    assert isinstance(reloaded, Jsonl)
    assert reloaded.owner_id == alice.id
    assert reloaded.key == f"users/{alice.id.hex}/{stored.id.hex}"
    assert other.key == f"users/{bob.id.hex}/{other.id.hex}"
    assert reloaded.name == "rollout.jsonl"
    assert await files.read(stored.id, owner_id=alice.id) == b'{"session":"native"}\n'
    assert await storage.read(reloaded.key) == b'{"session":"native"}\n'
    await files.delete(stored.id, owner_id=alice.id)
    with pytest.raises(FileNotFoundError):
        await files.get(stored.id, owner_id=alice.id)
    with pytest.raises(opendal.exceptions.NotFound):
        await storage.read(stored.key)
    assert await files.read(other.id, owner_id=bob.id) == b"another transcript"


@pytest.mark.parametrize("use_other_owner", [False, True])
async def test_owned_file_is_inaccessible_outside_owner_scope(
    owners: tuple[User, User],
    storage: opendal.AsyncOperator,
    use_other_owner: bool,
) -> None:
    alice, bob = owners
    files = FileManager(storage=storage)
    stored = await files.write(
        UploadFile(BytesIO(b"private"), filename="rollout.jsonl"), owner_id=alice.id
    )
    owner_id = bob.id if use_other_owner else None
    backend = Mock(wraps=storage)
    backend.read = AsyncMock()
    backend.delete = AsyncMock()
    scoped = FileManager(storage=backend)

    with pytest.raises(FileNotFoundError):
        await scoped.get(stored.id, owner_id=owner_id)
    with pytest.raises(FileNotFoundError):
        await scoped.read(stored.id, owner_id=owner_id)
    with pytest.raises(FileNotFoundError):
        async with scoped.copy(stored.id, owner_id=owner_id):
            pytest.fail("A different owner must not copy this file")
    with pytest.raises(FileNotFoundError):
        async with scoped.partial_copy(stored.id, end=3, owner_id=owner_id):
            pytest.fail("A different owner must not partially copy this file")
    with pytest.raises(FileNotFoundError):
        await scoped.delete(stored.id, owner_id=owner_id)

    backend.read.assert_not_awaited()
    backend.delete.assert_not_awaited()
    assert await files.read(stored.id, owner_id=alice.id) == b"private"


async def test_service_files_are_not_visible_in_user_scope(
    owners: tuple[User, User], storage: opendal.AsyncOperator
) -> None:
    alice, _ = owners
    files = FileManager(storage=storage)
    stored = await files.write(UploadFile(BytesIO(b"service"), filename="internal.txt"))

    assert stored.owner_id is None
    assert stored.key == stored.id.hex
    with pytest.raises(FileNotFoundError):
        await files.get(stored.id, owner_id=alice.id)
    with pytest.raises(FileNotFoundError):
        await files.read(stored.id, owner_id=alice.id)
    with pytest.raises(FileNotFoundError):
        async with files.copy(stored.id, owner_id=alice.id):
            pytest.fail("A user must not copy an unowned service file")
    with pytest.raises(FileNotFoundError):
        async with files.partial_copy(stored.id, end=3, owner_id=alice.id):
            pytest.fail("A user must not partially copy an unowned service file")
    with pytest.raises(FileNotFoundError):
        await files.delete(stored.id, owner_id=alice.id)
    assert await files.read(stored.id) == b"service"


async def test_missing_owner_leaves_no_content(
    in_memory_engine: AsyncEngine,
    storage: opendal.AsyncOperator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_id, owner_id = uuid7(), uuid7()
    monkeypatch.setattr("octomate.managers.files.uuid7", lambda: file_id)
    files = FileManager(storage=storage)

    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        await files.write(
            UploadFile(BytesIO(b"private"), filename="rollout.jsonl"), owner_id=owner_id
        )

    with pytest.raises(opendal.exceptions.NotFound):
        await storage.read(f"users/{owner_id.hex}/{file_id.hex}")
    with pytest.raises(FileNotFoundError):
        await files.get(file_id, owner_id=owner_id)


async def test_owner_deletion_cannot_turn_private_files_into_service_files(
    owners: tuple[User, User], storage: opendal.AsyncOperator
) -> None:
    alice, _ = owners
    files = FileManager(storage=storage)
    stored = await files.write(
        UploadFile(BytesIO(b"private"), filename="rollout.jsonl"), owner_id=alice.id
    )

    async with async_session() as session:
        owner = await session.one(User, expressions=[User["id"] == alice.id])
        await session.delete(owner)
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            await session.commit()

    assert await files.read(stored.id, owner_id=alice.id) == b"private"
    with pytest.raises(FileNotFoundError):
        await files.read(stored.id)
