"""The project registry: what resolves a working directory to a project, and how a
project comes to exist at all.

Nothing declares a project any more. A native session running where nothing claims
registers that directory (`ensure`), and `load` reads every row back — so a test that
wants a project either ensures one or persists the row a registration would have left.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.database import async_session
from octomate.managers.project import ProjectManager
from octomate.schemas.project import Project
from octomate.types.projects import ProjectOrigin
from tests.support.managers import a_registry


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


def directory(path: Path) -> Path:
    """A root need not exist, but a test about resolving one wants it real, since
    both sides are resolved before they are compared."""
    path.mkdir(parents=True, exist_ok=True)
    return path


async def find_project(name: str) -> Project | None:
    async with async_session() as session:
        return await session.one_or_none(
            Project,
            expressions=[Project["name"] == name],
        )


async def registered(root: Path, origin: ProjectOrigin = "codex") -> ProjectManager:
    """A registry holding one project at `root`, as a session there would leave it."""
    manager = ProjectManager()
    await manager.load()
    await manager.ensure(root, origin=origin)
    return manager


async def test_a_cwd_under_no_registered_root_resolves_to_nothing(
    tmp_path: Path,
) -> None:
    manager = await registered(directory(tmp_path / "inky"))

    assert manager.resolve(tmp_path / "elsewhere") is None


async def test_a_cwd_under_a_registered_root_resolves_to_that_project(
    tmp_path: Path,
) -> None:
    root = directory(tmp_path / "inky")
    manager = await registered(root)

    assert manager.resolve(root / "octomate" / "managers") == "inky"


async def test_the_root_itself_resolves(tmp_path: Path) -> None:
    root = directory(tmp_path / "inky")
    manager = await registered(root)

    assert manager.resolve(root) == "inky"


async def test_nested_roots_resolve_to_the_deeper_project(tmp_path: Path) -> None:
    # A monorepo and a package inside it are both projects; the specific one wins.
    mono = directory(tmp_path / "octoverse")
    manager = await a_registry(
        Project(root=mono), Project(root=directory(mono / "inky"))
    )

    assert manager.resolve(mono / "inky" / "octomate") == "inky"
    assert manager.resolve(mono / "kraken") == "octoverse"


async def test_a_cwd_under_an_extra_root_resolves(tmp_path: Path) -> None:
    manager = await a_registry(
        Project(
            root=directory(tmp_path / "inky"),
            extra_roots=[directory(tmp_path / "vscode")],
        )
    )

    assert manager.resolve(tmp_path / "vscode" / "User") == "inky"


async def test_a_root_written_with_a_tilde_expands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pydantic keeps `~/...` literal, so an unexpanded root would match nothing.
    monkeypatch.setenv("HOME", str(tmp_path))
    directory(tmp_path / "Projects" / "inky")
    manager = await a_registry(Project(root=Path("~/Projects/inky")))

    assert manager.resolve(tmp_path / "Projects" / "inky" / "octomate") == "inky"


async def test_a_relative_root_is_made_absolute() -> None:
    # A relative root would otherwise hang off wherever Octomate was started.
    project = Project(root=Path("octomate"))

    assert project.root == Path.cwd() / "octomate"


async def test_a_root_written_as_a_url_is_refused() -> None:
    # `Path("file:///srv/inky")` is a real relative path that matches nothing, so a
    # url has to be refused rather than quietly converted.
    # Validated from a string: `PurePath` collapses `://` to `:/`, so a `Path` built
    # first would have hidden the scheme before the check could see it.
    with pytest.raises(ValidationError, match="a root is a plain local path"):
        Project.model_validate({"root": "file:///srv/inky"})


async def test_the_filesystem_root_is_refused() -> None:
    # It is a directory and it exists, but it has no name to be called by — the one
    # case that would leave a project unnameable.
    with pytest.raises(ValidationError, match="is the filesystem root"):
        Project(root=Path("/"))


async def test_a_root_containing_a_space_is_stored_verbatim(tmp_path: Path) -> None:
    # It was percent-encoded while roots were urls; nothing encodes it now.
    root = directory(tmp_path / "Application Support" / "Code")
    manager = await a_registry(Project(name="vscode", root=root))

    assert manager.projects["vscode"].root == root
    assert manager.resolve(root / "User") == "vscode"


async def test_a_symlinked_cwd_resolves_to_the_real_root(tmp_path: Path) -> None:
    # `is_relative_to` is lexical, so both sides have to be resolved first.
    root = tmp_path / "real"
    (root / "octomate").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(root)
    manager = await a_registry(Project(name="inky", root=root))

    assert manager.resolve(link / "octomate") == "inky"


async def test_a_symlinked_root_resolves(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "octomate").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    manager = await a_registry(Project(name="inky", root=link))

    assert manager.resolve(real / "octomate") == "inky"


async def test_a_root_reached_through_a_symlinked_tmp_resolves() -> None:
    # /tmp is a symlink to /private/tmp on macOS: the exact miss the ticket names.
    manager = await a_registry(Project(name="scratch", root=Path("/tmp")))

    assert manager.resolve(Path("/tmp/whatever")) == "scratch"
    assert manager.resolve(Path("/private/tmp/whatever")) == "scratch"


async def test_a_root_that_is_not_on_disk_still_resolves(tmp_path: Path) -> None:
    # A project outlives the directory it was found at, and the same type rehydrates
    # every row — so one deleted checkout must not stop the registry loading.
    manager = await a_registry(Project(root=tmp_path / "deleted"))

    assert manager.resolve(tmp_path / "deleted" / "src") == "deleted"


async def test_load_reads_every_row_back(tmp_path: Path) -> None:
    root = directory(tmp_path / "inky")
    await registered(root)

    fresh = ProjectManager()
    await fresh.load()

    assert set(fresh.projects) == {"inky"}
    assert fresh.resolve(root / "octomate") == "inky"


async def test_get_and_list_read_the_cache(tmp_path: Path) -> None:
    manager = await a_registry(
        Project(root=directory(tmp_path / "inky")),
        Project(root=directory(tmp_path / "kraken")),
    )

    inky = manager.get("inky")
    assert inky is not None
    assert inky.root == tmp_path / "inky"
    assert {project.name for project in manager.list()} == {"inky", "kraken"}


async def test_get_is_none_for_a_name_nothing_is_registered_as(tmp_path: Path) -> None:
    manager = await registered(directory(tmp_path / "inky"))

    assert manager.get("kraken") is None


async def test_a_project_is_named_after_its_root_directory(tmp_path: Path) -> None:
    manager = await registered(directory(tmp_path / "octoverse" / "kraken"))

    assert set(manager.projects) == {"kraken"}


async def test_ensure_registers_a_directory_no_project_holds(tmp_path: Path) -> None:
    manager = await a_registry()

    project = await manager.ensure(directory(tmp_path / "inky"), origin="codex")

    assert project.name == "inky"
    assert project.root == tmp_path / "inky"
    assert project.origin == "codex"
    assert await find_project("inky") is not None
    assert manager.resolve(tmp_path / "inky" / "octomate") == "inky"


async def test_ensure_inside_a_registered_project_returns_it(tmp_path: Path) -> None:
    # The run is simply below its project's root, which is where runs are allowed to
    # be — every package of a monorepo becoming its own project is the failure here.
    root = directory(tmp_path / "inky")
    manager = await a_registry(Project(root=root))

    project = await manager.ensure(
        directory(root / "octomate" / "managers"), origin="codex"
    )

    assert project.name == "inky"
    assert set(manager.projects) == {"inky"}


async def test_ensure_is_idempotent(tmp_path: Path) -> None:
    manager = await a_registry()
    root = directory(tmp_path / "inky")

    first = await manager.ensure(root, origin="codex")
    second = await manager.ensure(root / "octomate", origin="codex")

    assert first.id == second.id
    assert set(manager.projects) == {"inky"}


async def test_ensure_renames_around_a_name_already_taken(tmp_path: Path) -> None:
    # Two directories really can be called the same thing, and the root is the
    # identity, so the newcomer steps aside rather than being refused.
    manager = await a_registry(Project(root=directory(tmp_path / "vita" / "api")))

    second = await manager.ensure(directory(tmp_path / "api"), origin="codex")

    assert second.name == "api-2"
    assert manager.resolve(tmp_path / "api" / "src") == "api-2"
    assert manager.resolve(tmp_path / "vita" / "api" / "src") == "api"


async def test_ensure_above_a_registered_project_keeps_the_deeper_one(
    tmp_path: Path,
) -> None:
    inner = directory(tmp_path / "octoverse" / "inky")
    manager = await a_registry(Project(root=inner))

    outer = await manager.ensure(directory(tmp_path / "octoverse"), origin="codex")

    assert outer.name == "octoverse"
    assert manager.resolve(inner / "octomate") == "inky"
    assert manager.resolve(tmp_path / "octoverse" / "kraken") == "octoverse"


async def test_an_empty_registry_resolves_nothing(tmp_path: Path) -> None:
    manager = await a_registry()

    assert manager.resolve(tmp_path) is None
    assert manager.list() == []
