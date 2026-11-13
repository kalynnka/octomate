"""Logical volume migrations preserve data and refuse lossy conversions."""

from importlib import import_module
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.schemas.volumes import GitVolume, Volume
from octomate.schemas.volumes.storage import HostDirectory
from tests.support.managers import a_project, a_registry, a_thread
from tests.support.users import a_user

migration = import_module(
    "octomate.migrations.versions.2026_11_13_0047-d5553eabfc92_separate_logical_volume_kind_and_usage"
)


storage_migration = import_module(
    "octomate.migrations.versions.2026_11_13_2105-afaf6db24028_persist_volume_storage_locations"
)


mirror_migration = import_module(
    "octomate.migrations.versions.2026_11_13_2131-25d11a963652_record_service_mirrors_and_volume_"
)


async def test_storage_migration_round_trip(in_memory_engine: AsyncEngine) -> None:
    def round_trip(connection: Connection) -> None:
        with Operations.context(MigrationContext.configure(connection)):
            mirror_migration.downgrade()
            storage_migration.downgrade()
            assert "storage" not in {
                column["name"] for column in inspect(connection).get_columns("volumes")
            }
            storage_migration.upgrade()
        storage = next(
            column
            for column in inspect(connection).get_columns("volumes")
            if column["name"] == "storage"
        )
        assert not storage["nullable"]
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []

    async with in_memory_engine.begin() as connection:
        await connection.run_sync(round_trip)


async def test_legacy_volumes_preserve_data_and_paths_in_both_directions(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> None:
    owner = await a_user()
    thread_id = await a_thread()
    project = a_project(tmp_path / "project")
    await a_registry(project)
    records = [
        GitVolume(
            storage=HostDirectory(Path("storage") / str(uuid7())),
            owner_id=owner.id,
            usage="tree",
            thread_id=thread_id,
            project_id=project.id,
            base_ref="refs/heads/feature",
        ),
        GitVolume(
            storage=HostDirectory(Path("storage") / str(uuid7())),
            owner_id=owner.id,
            usage="tree",
            thread_id=thread_id,
            ephemeral=True,
        ),
        Volume(
            storage=HostDirectory(Path("storage") / str(uuid7())),
            owner_id=owner.id,
            usage="cache",
        ),
        Volume(
            storage=HostDirectory(Path("storage") / str(uuid7())),
            owner_id=owner.id,
            usage="memory",
        ),
    ]
    async with async_session() as session:
        session.add_all(records)
        await session.commit()

    def round_trip(connection: Connection) -> None:
        with Operations.context(MigrationContext.configure(connection)):
            mirror_migration.downgrade()
            storage_migration.downgrade()
            before = (
                connection.execute(text("SELECT * FROM volumes ORDER BY id"))
                .mappings()
                .all()
            )
            migration.downgrade()
            legacy = (
                connection.execute(text("SELECT * FROM volumes ORDER BY id"))
                .mappings()
                .all()
            )
            assert [row["kind"] for row in legacy] == [row["usage"] for row in before]
            assert [(row["owner_id"], row["kind"], row["id"]) for row in legacy] == [
                (row["owner_id"], row["usage"], row["id"]) for row in before
            ]
            migration.upgrade()
        after = (
            connection.execute(text("SELECT * FROM volumes ORDER BY id"))
            .mappings()
            .all()
        )
        assert before == after
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []

    async with in_memory_engine.begin() as connection:
        await connection.run_sync(round_trip)


@pytest.mark.parametrize(
    "case", ["unscoped", "git_cache", "ephemeral_cache", "plain_tree"]
)
async def test_downgrade_rejects_loss_without_changing_data(
    in_memory_engine: AsyncEngine, case: str
) -> None:
    owner = await a_user()
    if case == "git_cache":
        volume = GitVolume(
            storage=HostDirectory(Path("storage") / str(uuid7())),
            owner_id=owner.id,
            usage="cache",
        )
    elif case == "plain_tree":
        volume = Volume(
            storage=HostDirectory(Path("storage") / str(uuid7())),
            owner_id=owner.id,
            usage="tree",
            thread_id=await a_thread(),
        )
    else:
        volume = Volume(
            storage=HostDirectory(Path("storage") / str(uuid7())),
            owner_id=owner.id,
            usage="cache" if case == "ephemeral_cache" else None,
            ephemeral=case == "ephemeral_cache",
        )
    async with async_session() as session:
        session.add(volume)
        await session.commit()

    def reject(connection: Connection) -> None:
        before = connection.execute(text("SELECT * FROM volumes")).mappings().all()
        with (
            Operations.context(MigrationContext.configure(connection)),
            pytest.raises(ValueError, match="cannot be represented"),
        ):
            migration.downgrade()
        assert (
            connection.execute(text("SELECT * FROM volumes")).mappings().all() == before
        )

    async with in_memory_engine.begin() as connection:
        await connection.run_sync(reject)


async def test_upgrade_refuses_unknown_legacy_kinds_before_dropping_the_table(
    in_memory_engine: AsyncEngine,
) -> None:
    owner = await a_user()
    async with async_session() as session:
        session.add(
            Volume(
                storage=HostDirectory(Path("storage") / str(uuid7())),
                owner_id=owner.id,
                usage="memory",
            )
        )
        await session.commit()

    def reject(connection: Connection) -> None:
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            connection.execute(text("UPDATE volumes SET kind = 'unknown'"))
            before = connection.execute(text("SELECT * FROM volumes")).mappings().all()
            with pytest.raises(ValueError, match="unknown legacy volume kind"):
                migration.upgrade()
            assert (
                connection.execute(text("SELECT * FROM volumes")).mappings().all()
                == before
            )

    async with in_memory_engine.begin() as connection:
        await connection.run_sync(reject)


async def test_mirror_schema_round_trip(in_memory_engine: AsyncEngine) -> None:
    def round_trip(connection: Connection) -> None:
        with Operations.context(MigrationContext.configure(connection)):
            mirror_migration.downgrade()
            mirror_migration.upgrade()
        columns = {
            column["name"]: column
            for column in inspect(connection).get_columns("volumes")
        }
        assert columns["owner_id"]["nullable"]
        assert "mirror_id" in columns
        assert {
            constraint["name"]
            for constraint in inspect(connection).get_check_constraints("volumes")
        } >= {
            "ck_volumes_persisted_owner",
            "ck_volumes_mirror_source",
            "ck_volumes_usage_scope",
        }
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []

    async with in_memory_engine.begin() as connection:
        await connection.run_sync(round_trip)
