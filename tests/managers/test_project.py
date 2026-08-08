from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.config.projects import ConfigProject
from octomate.database import async_session
from octomate.managers.project import ProjectManager
from octomate.schemas.project import Project

config_projects: TypeAdapter[list[Project]] = TypeAdapter(list[ConfigProject])


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


def directory(path: Path) -> Path:
    """A declared root has to exist, so a test that declares one makes it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def config(spec: Sequence[Mapping[str, object]]) -> list[Project]:
    """The `projects:` block as the config layer hands it over."""
    return config_projects.validate_python(spec)


async def find_project(name: str) -> Project | None:
    async with async_session() as session:
        return await session.one_or_none(
            Project,
            expressions=[Project["name"] == name],
        )


async def registry(spec: Sequence[Mapping[str, object]]) -> ProjectManager:
    manager = ProjectManager(config(spec))
    await manager.reconcile()
    return manager


async def test_a_cwd_under_no_declared_root_resolves_to_nothing(
    tmp_path: Path,
) -> None:
    manager = await registry([{"root": str(directory(tmp_path / "inky"))}])

    assert manager.resolve(tmp_path / "elsewhere") is None


async def test_a_cwd_under_a_declared_root_resolves_to_that_project(
    tmp_path: Path,
) -> None:
    root = directory(tmp_path / "inky")
    manager = await registry([{"root": str(root)}])

    assert manager.resolve(root / "octomate" / "managers") == "inky"


async def test_the_root_itself_resolves(tmp_path: Path) -> None:
    root = directory(tmp_path / "inky")
    manager = await registry([{"root": str(root)}])

    assert manager.resolve(root) == "inky"


async def test_nested_roots_resolve_to_the_deeper_project(tmp_path: Path) -> None:
    # A monorepo and a package inside it are both projects; the specific one wins.
    mono = directory(tmp_path / "octoverse")
    manager = await registry(
        [{"root": str(mono)}, {"root": str(directory(mono / "inky"))}]
    )

    assert manager.resolve(mono / "inky" / "octomate") == "inky"
    assert manager.resolve(mono / "kraken") == "octoverse"


async def test_a_cwd_under_an_extra_root_resolves(tmp_path: Path) -> None:
    manager = await registry(
        [
            {
                "root": str(directory(tmp_path / "inky")),
                "extra_roots": [str(directory(tmp_path / "vscode"))],
            }
        ]
    )

    assert manager.resolve(tmp_path / "vscode" / "User") == "inky"


async def test_a_root_declared_with_a_tilde_expands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pydantic keeps `~/...` literal, so an unexpanded root would match nothing.
    monkeypatch.setenv("HOME", str(tmp_path))
    directory(tmp_path / "Projects" / "inky")
    manager = await registry([{"root": "~/Projects/inky"}])

    assert manager.resolve(tmp_path / "Projects" / "inky" / "octomate") == "inky"


async def test_a_root_containing_a_space_is_stored_verbatim(tmp_path: Path) -> None:
    # It was percent-encoded while roots were urls; nothing encodes it now.
    root = directory(tmp_path / "Application Support" / "Code")
    manager = await registry([{"name": "vscode", "root": str(root)}])

    assert manager.projects["vscode"].root == root
    assert manager.resolve(root / "User") == "vscode"


async def test_a_symlinked_cwd_resolves_to_the_real_root(tmp_path: Path) -> None:
    # `is_relative_to` is lexical, so both sides have to be resolved first.
    root = tmp_path / "real"
    (root / "octomate").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(root)
    manager = await registry([{"name": "inky", "root": str(root)}])

    assert manager.resolve(link / "octomate") == "inky"


async def test_a_symlinked_root_declaration_resolves(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "octomate").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    manager = await registry([{"name": "inky", "root": str(link)}])

    assert manager.resolve(real / "octomate") == "inky"


async def test_a_root_reached_through_a_symlinked_tmp_resolves() -> None:
    # /tmp is a symlink to /private/tmp on macOS: the exact miss the ticket names.
    manager = await registry([{"name": "scratch", "root": "/tmp"}])

    assert manager.resolve(Path("/tmp/whatever")) == "scratch"
    assert manager.resolve(Path("/private/tmp/whatever")) == "scratch"


async def test_reconcile_persists_the_declared_block(tmp_path: Path) -> None:
    manager = await registry(
        [
            {
                "root": str(directory(tmp_path / "inky")),
                "extra_roots": [str(directory(tmp_path / "vscode"))],
                "description": "Octomate itself.",
                "permission_mode": "accept_edits",
            }
        ]
    )

    stored = await find_project("inky")
    assert stored is not None
    assert stored.root == tmp_path / "inky"
    assert stored.extra_roots == [tmp_path / "vscode"]
    assert stored.description == "Octomate itself."
    assert stored.permission_mode == "accept_edits"
    assert manager.projects["inky"].id == stored.id


async def test_reconcile_is_idempotent_and_updates_by_stable_name(
    tmp_path: Path,
) -> None:
    original = await registry(
        [
            {
                "name": "inky",
                "root": str(directory(tmp_path / "old")),
                "description": "The old one.",
            }
        ]
    )
    first = await find_project("inky")
    assert first is not None

    await original.reconcile()
    moved = await registry(
        [
            {
                "name": "inky",
                "root": str(directory(tmp_path / "new")),
                "description": "The new one.",
            }
        ]
    )

    current = await find_project("inky")
    assert current is not None
    assert current.id == first.id
    assert current.root == tmp_path / "new"
    assert current.description == "The new one."
    assert moved.resolve(tmp_path / "new" / "x") == "inky"
    assert moved.resolve(tmp_path / "old" / "x") is None


async def test_removing_a_project_retains_the_row_and_stops_it_resolving(
    tmp_path: Path,
) -> None:
    root = directory(tmp_path / "inky")
    await registry([{"root": str(root)}])

    emptied = await registry([])

    # History may already name it, so the row stays and can be re-declared later.
    assert await find_project("inky") is not None
    assert emptied.resolve(root / "octomate") is None
    assert emptied.projects == {}


async def test_get_and_list_read_the_cache(tmp_path: Path) -> None:
    manager = await registry(
        [
            {"root": str(directory(tmp_path / "inky"))},
            {"root": str(directory(tmp_path / "kraken"))},
        ]
    )

    inky = manager.get("inky")
    assert inky is not None
    assert inky.root == tmp_path / "inky"
    # Declaration order, so a listing reads the way the config does.
    assert [project.name for project in manager.list()] == ["inky", "kraken"]


async def test_get_is_none_for_a_name_nothing_is_registered_as(tmp_path: Path) -> None:
    manager = await registry([{"root": str(directory(tmp_path / "inky"))}])

    assert manager.get("kraken") is None


async def test_get_is_none_once_a_declaration_is_removed(tmp_path: Path) -> None:
    # The row is retained, but the registry is what YAML currently declares.
    await registry([{"root": str(directory(tmp_path / "inky"))}])

    emptied = await registry([])

    assert await find_project("inky") is not None
    assert emptied.get("inky") is None
    assert emptied.list() == []


async def test_a_project_is_named_after_its_root_directory(tmp_path: Path) -> None:
    manager = await registry(
        [{"root": str(directory(tmp_path / "octoverse" / "kraken"))}]
    )

    assert set(manager.projects) == {"kraken"}


async def test_a_declared_name_beats_the_root_directory(tmp_path: Path) -> None:
    manager = await registry(
        [{"name": "krak", "root": str(directory(tmp_path / "kraken"))}]
    )

    assert set(manager.projects) == {"krak"}
    assert manager.resolve(tmp_path / "kraken" / "src") == "krak"


async def test_two_projects_sharing_a_name_are_refused_and_write_nothing(
    tmp_path: Path,
) -> None:
    # Two checkouts of one repo derive the same name from their directories.
    manager = ProjectManager(
        config(
            [
                {"root": str(directory(tmp_path / "a" / "inky"))},
                {"root": str(directory(tmp_path / "b" / "inky"))},
            ]
        )
    )

    with pytest.raises(ValueError, match="declared twice"):
        await manager.reconcile()

    assert await find_project("inky") is None


async def test_two_projects_on_one_root_are_refused_and_write_nothing(
    tmp_path: Path,
) -> None:
    root = directory(tmp_path / "shared")
    manager = ProjectManager(
        config([{"name": "a", "root": str(root)}, {"name": "b", "root": str(root)}])
    )

    with pytest.raises(ValueError, match="declared for both"):
        await manager.reconcile()

    assert await find_project("a") is None
    assert await find_project("b") is None


async def test_an_extra_root_colliding_with_another_root_is_refused(
    tmp_path: Path,
) -> None:
    shared = directory(tmp_path / "shared")
    manager = ProjectManager(
        config(
            [
                {
                    "name": "a",
                    "root": str(directory(tmp_path / "a")),
                    "extra_roots": [str(shared)],
                },
                {"name": "b", "root": str(shared)},
            ]
        )
    )

    with pytest.raises(ValueError, match="declared for both"):
        await manager.reconcile()


async def test_a_root_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="does not point to a directory"):
        config([{"root": str(tmp_path / "not-cloned-yet")}])


async def test_a_manager_with_no_config_resolves_nothing(tmp_path: Path) -> None:
    manager = ProjectManager()
    await manager.reconcile()

    assert manager.resolve(tmp_path) is None
