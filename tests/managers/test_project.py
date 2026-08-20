"""The project registry: what resolves a working directory to a project, and how a
project comes to exist at all.

One way in: the operator declares a project in `projects:` and `reconcile` upserts it
by root at startup. `load` reads every row back without reconciling — so a test that
wants a project declares one, or persists the row a declaration would have left.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.database import async_session
from octomate.managers.project import ProjectManager
from octomate.schemas.project import DirectoryUpstream, Project, RemoteUpstream
from tests.support.managers import a_project, a_registry

# Built at import, so nothing has entered `sqlalchemy_materia` yet — the state a
# config-declared project is really in by the time startup reconciles it.
UNBOUND = Project.Create(
    root=Path("/tmp"), upstream=DirectoryUpstream(path=Path("/tmp"))
)


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


async def test_a_cwd_under_no_registered_root_resolves_to_nothing(
    tmp_path: Path,
) -> None:
    manager = await a_registry(a_project(directory(tmp_path / "inky")))

    assert manager.resolve(tmp_path / "elsewhere") is None


async def test_a_cwd_under_a_registered_root_resolves_to_that_project(
    tmp_path: Path,
) -> None:
    root = directory(tmp_path / "inky")
    manager = await a_registry(a_project(root))

    assert manager.resolve(root / "octomate" / "managers") == "inky"


async def test_the_root_itself_resolves(tmp_path: Path) -> None:
    root = directory(tmp_path / "inky")
    manager = await a_registry(a_project(root))

    assert manager.resolve(root) == "inky"


async def test_resolving_registers_nothing(tmp_path: Path) -> None:
    # OCTO-45: sessions stopped registering projects, so resolution is a pure read —
    # a miss mints no project, and a hit does not duplicate one.
    manager = await a_registry(a_project(directory(tmp_path / "inky")))

    assert manager.resolve(tmp_path / "elsewhere") is None
    assert manager.resolve(tmp_path / "inky" / "src") == "inky"

    assert set(manager.projects) == {"inky"}
    assert await find_project("elsewhere") is None


async def test_nested_roots_resolve_to_the_deeper_project(tmp_path: Path) -> None:
    # A monorepo and a package inside it are both projects; the specific one wins.
    mono = directory(tmp_path / "octoverse")
    manager = await a_registry(a_project(mono), a_project(directory(mono / "inky")))

    assert manager.resolve(mono / "inky" / "octomate") == "inky"
    assert manager.resolve(mono / "kraken") == "octoverse"


async def test_a_cwd_under_an_extra_root_resolves(tmp_path: Path) -> None:
    manager = await a_registry(
        a_project(
            directory(tmp_path / "inky"),
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
    manager = await a_registry(a_project(Path("~/Projects/inky")))

    assert manager.resolve(tmp_path / "Projects" / "inky" / "octomate") == "inky"


async def test_a_relative_root_is_made_absolute() -> None:
    # A relative root would otherwise hang off wherever Octomate was started.
    project = a_project(Path("octomate"))

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
        Project(root=Path("/"), upstream=DirectoryUpstream(path=Path("/tmp")))


async def test_a_project_without_an_upstream_is_refused(tmp_path: Path) -> None:
    # Required on purpose: where a mirror syncs from is the declaration's to say,
    # and a guessed root-fallback would hide a missing one.
    with pytest.raises(ValidationError, match="upstream"):
        Project.model_validate({"root": str(tmp_path / "inky")})


async def test_a_remote_upstream_round_trips(tmp_path: Path) -> None:
    upstream = RemoteUpstream(url="git@github.com:octoverse/inky.git")
    await a_registry(Project(root=directory(tmp_path / "inky"), upstream=upstream))

    stored = await find_project("inky")
    assert stored is not None
    assert stored.upstream == upstream


async def test_a_directory_upstream_round_trips(tmp_path: Path) -> None:
    upstream = DirectoryUpstream(path=directory(tmp_path / "elsewhere"))
    await a_registry(Project(root=directory(tmp_path / "inky"), upstream=upstream))

    stored = await find_project("inky")
    assert stored is not None
    assert stored.upstream == upstream


async def test_an_unknown_upstream_kind_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="does not match any of the expected"):
        Project.model_validate(
            {
                "root": str(tmp_path / "inky"),
                "upstream": {"kind": "svn", "url": "svn://elsewhere"},
            }
        )


async def test_a_root_containing_a_space_is_stored_verbatim(tmp_path: Path) -> None:
    # It was percent-encoded while roots were urls; nothing encodes it now.
    root = directory(tmp_path / "Application Support" / "Code")
    manager = await a_registry(a_project(root, name="vscode"))

    assert manager.projects["vscode"].root == root
    assert manager.resolve(root / "User") == "vscode"


async def test_a_symlinked_cwd_resolves_to_the_real_root(tmp_path: Path) -> None:
    # `is_relative_to` is lexical, so both sides have to be resolved first.
    root = tmp_path / "real"
    (root / "octomate").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(root)
    manager = await a_registry(a_project(root, name="inky"))

    assert manager.resolve(link / "octomate") == "inky"


async def test_a_symlinked_root_resolves(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "octomate").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    manager = await a_registry(a_project(link, name="inky"))

    assert manager.resolve(real / "octomate") == "inky"


async def test_a_root_reached_through_a_symlinked_tmp_resolves() -> None:
    # /tmp is a symlink to /private/tmp on macOS: the exact miss the ticket names.
    manager = await a_registry(a_project(Path("/tmp"), name="scratch"))

    assert manager.resolve(Path("/tmp/whatever")) == "scratch"
    assert manager.resolve(Path("/private/tmp/whatever")) == "scratch"


async def test_a_root_that_is_not_on_disk_still_resolves(tmp_path: Path) -> None:
    # A project outlives the directory it was found at, and the same type rehydrates
    # every row — so one deleted checkout must not stop the registry loading.
    manager = await a_registry(a_project(tmp_path / "deleted"))

    assert manager.resolve(tmp_path / "deleted" / "src") == "deleted"


async def test_load_reads_every_row_back(tmp_path: Path) -> None:
    root = directory(tmp_path / "inky")
    await a_registry(a_project(root))

    fresh = ProjectManager()
    await fresh.load()

    assert set(fresh.projects) == {"inky"}
    assert fresh.resolve(root / "octomate") == "inky"


async def test_get_and_list_read_the_cache(tmp_path: Path) -> None:
    manager = await a_registry(
        a_project(directory(tmp_path / "inky")),
        a_project(directory(tmp_path / "kraken")),
    )

    inky = manager.get("inky")
    assert inky is not None
    assert inky.root == tmp_path / "inky"
    assert {project.name for project in manager.list()} == {"inky", "kraken"}


async def test_get_is_none_for_a_name_nothing_is_registered_as(tmp_path: Path) -> None:
    manager = await a_registry(a_project(directory(tmp_path / "inky")))

    assert manager.get("kraken") is None


async def test_a_project_is_named_after_its_root_directory(tmp_path: Path) -> None:
    manager = await a_registry(a_project(directory(tmp_path / "octoverse" / "kraken")))

    assert set(manager.projects) == {"kraken"}


async def test_an_empty_registry_resolves_nothing(tmp_path: Path) -> None:
    manager = await a_registry()

    assert manager.resolve(tmp_path) is None
    assert manager.list() == []


# --- reconcile ---------------------------------------------------------------------


async def reconciled(**declared: Project.Create) -> ProjectManager:
    """A registry over the declared block, reconciled the way startup does it."""
    manager = ProjectManager(declared)
    await manager.reconcile()
    return manager


async def test_a_declaration_built_before_the_materia_still_persists() -> None:
    # Production validates its config in `main.py`, before `Octomate.app()` enters
    # `sqlalchemy_materia`, so a declared `Project` has no row behind it to add to a
    # session. UNBOUND is built the same way — at import, before the engine fixture —
    # which the in-test declarations above cannot reproduce.
    manager = ProjectManager({"scratch": UNBOUND})

    await manager.reconcile()

    assert manager.resolve(Path("/tmp/whatever")) == "scratch"
    assert await find_project("scratch") is not None


async def test_a_declared_project_is_registered_and_resolves(tmp_path: Path) -> None:
    root = directory(tmp_path / "inky")

    manager = await reconciled(
        inky=Project.Create(root=root, upstream=DirectoryUpstream(path=root))
    )

    assert manager.resolve(root / "octomate") == "inky"
    assert (await find_project("inky")) is not None


async def test_reconcile_updates_a_declaration_in_place(tmp_path: Path) -> None:
    root = directory(tmp_path / "inky")
    settings = directory(tmp_path / "vscode")
    await reconciled(
        inky=Project.Create(root=root, upstream=DirectoryUpstream(path=root))
    )

    manager = await reconciled(
        inky=Project.Create(
            root=root,
            extra_roots=[settings],
            description="Octomate.",
            upstream=DirectoryUpstream(path=root),
        )
    )

    project = manager.get("inky")
    assert project is not None
    assert project.description == "Octomate."
    assert manager.resolve(settings / "User") == "inky"


async def test_reconcile_carries_a_redeclared_upstream(tmp_path: Path) -> None:
    # The declared block is the source of truth for a declared project, so editing
    # its upstream in YAML has to reach the row — silently keeping the old one is
    # the failure here.
    root = directory(tmp_path / "inky")
    await reconciled(
        inky=Project.Create(root=root, upstream=DirectoryUpstream(path=root))
    )

    upstream = RemoteUpstream(url="git@github.com:octoverse/inky.git")
    manager = await reconciled(inky=Project.Create(root=root, upstream=upstream))

    project = manager.get("inky")
    assert project is not None
    assert project.upstream == upstream
    stored = await find_project("inky")
    assert stored is not None
    assert stored.upstream == upstream


async def test_declaring_a_registered_directory_adopts_its_row(
    tmp_path: Path,
) -> None:
    # The root is the identity and the unique column, so a declaration renames the row
    # an earlier block left rather than racing it to a second row for one directory.
    root = directory(tmp_path / "octoverse" / "inky")
    earlier = await a_registry(a_project(root))
    [existing] = earlier.list()

    manager = await reconciled(
        octomate=Project.Create(root=root, upstream=DirectoryUpstream(path=root))
    )

    assert [project.id for project in manager.list()] == [existing.id]
    assert manager.resolve(root / "octomate") == "octomate"


async def test_an_undeclared_project_is_left_alone(tmp_path: Path) -> None:
    # The threads and runs recorded under a row still name it, so dropping a
    # declaration is not a request to forget the history filed there.
    root = directory(tmp_path / "kraken")
    await a_registry(a_project(root))

    inky = directory(tmp_path / "inky")
    manager = await reconciled(
        inky=Project.Create(root=inky, upstream=DirectoryUpstream(path=inky))
    )

    assert manager.resolve(root / "src") == "kraken"


async def test_a_root_disk_has_lost_is_disabled(tmp_path: Path) -> None:
    root = directory(tmp_path / "inky")
    await reconciled(
        inky=Project.Create(root=root, upstream=DirectoryUpstream(path=root))
    )
    root.rmdir()

    manager = await reconciled(
        inky=Project.Create(root=root, upstream=DirectoryUpstream(path=root))
    )

    assert manager.resolve(root / "octomate") is None
    project = manager.get("inky")
    # The row survives, so the threads and runs naming it still read back.
    assert project is not None
    assert project.enabled is False


async def test_a_project_re_enables_when_its_directory_is_back(
    tmp_path: Path,
) -> None:
    root = tmp_path / "inky"
    await reconciled(
        inky=Project.Create(root=root, upstream=DirectoryUpstream(path=root))
    )
    directory(root)

    manager = await reconciled(
        inky=Project.Create(root=root, upstream=DirectoryUpstream(path=root))
    )

    assert manager.resolve(root / "octomate") == "inky"


async def test_an_undeclared_project_is_disabled_when_its_root_goes(
    tmp_path: Path,
) -> None:
    # The block no longer declares it, and it is still swept: the flag is about disk,
    # not YAML.
    root = directory(tmp_path / "kraken")
    await a_registry(a_project(root))
    root.rmdir()

    manager = await reconciled()

    assert manager.resolve(root / "src") is None
    assert set(manager.projects) == {"kraken"}


async def test_two_declarations_at_one_root_are_refused(tmp_path: Path) -> None:
    root = directory(tmp_path / "inky")

    with pytest.raises(ValueError, match="claimed by both"):
        await reconciled(
            inky=Project.Create(root=root, upstream=DirectoryUpstream(path=root)),
            octomate=Project.Create(root=root, upstream=DirectoryUpstream(path=root)),
        )


async def test_an_extra_root_another_project_already_holds_is_refused(
    tmp_path: Path,
) -> None:
    # Nothing constrains `extra_roots`, and a directory two projects claim resolves to
    # whichever sorted first — so the check runs over the registry, not the block.
    kraken = directory(tmp_path / "kraken")
    await a_registry(a_project(kraken))

    inky = directory(tmp_path / "inky")
    with pytest.raises(ValueError, match="claimed by both 'kraken' and 'inky'"):
        await reconciled(
            inky=Project.Create(
                root=inky,
                extra_roots=[kraken],
                upstream=DirectoryUpstream(path=inky),
            )
        )


async def test_a_declared_name_a_registered_project_already_holds_is_refused(
    tmp_path: Path,
) -> None:
    # Ahead of the unique constraint, which would not say which two directories
    # wanted the name.
    await a_registry(a_project(directory(tmp_path / "vita" / "api")))

    api = directory(tmp_path / "api")
    with pytest.raises(ValueError, match="both named 'api'"):
        await reconciled(
            api=Project.Create(root=api, upstream=DirectoryUpstream(path=api))
        )
