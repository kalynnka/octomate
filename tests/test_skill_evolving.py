from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from octomate.config import SkillConfig
from octomate.tentacles.agent.skills.dedup import SkillDeduper, SkillDraft
from octomate.tentacles.agent.skills.evolving import build_skill_evolving_toolset
from octomate.tentacles.agent.skills.fuzzy import apply_patch, find_match
from octomate.tentacles.agent.skills.library import SkillLibrary
from octomate.tentacles.agent.skills.writer import AtomicSkillWriter

SKILL_MD = textwrap.dedent("""\
    ---
    name: test-skill
    description: A test skill for unit testing.
    version: 1.0.0
    ---

    # Test Skill

    Do the thing when asked.

    ## Steps

    1. Do step one.
    2. Do step two.
""")

BUNDLED_MD = textwrap.dedent("""\
    ---
    name: bundled-skill
    description: A bundled skill the agent must not delete.
    version: 1.0.0
    ---

    Bundled content.
""")


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def user_root(tmp_path: Path) -> Path:
    return tmp_path / "user_skills"


@pytest.fixture
def bundled_root(tmp_path: Path) -> Path:
    root = tmp_path / "octoskills"
    skill_dir = root / "bundled-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(BUNDLED_MD)
    return root


@pytest.fixture
def skill_root(tmp_path: Path) -> Path:
    root = tmp_path / "user_skills"
    skill_dir = root / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD)
    return root


@pytest.fixture
def library(skill_root: Path) -> SkillLibrary:
    lib = SkillLibrary([skill_root])
    lib.discover()
    return lib


@pytest.fixture
def writer(user_root: Path) -> AtomicSkillWriter:
    user_root.mkdir(parents=True, exist_ok=True)
    return AtomicSkillWriter(user_root)


# ── AtomicSkillWriter ────────────────────────────────────────────────────────


def test_atomic_create_commits_all_files(tmp_path: Path) -> None:
    user_root = tmp_path / "skills"
    user_root.mkdir()
    library = SkillLibrary([user_root])
    library.discover()
    writer = AtomicSkillWriter(user_root)

    files = {
        "SKILL.md": b"---\nname: new-skill\ndescription: New.\n---\n\nBody.\n",
        "references/api.md": b"# API\nDetails here.",
        "scripts/build.py": b"print('hello')",
    }
    writer.commit("new-skill", files, library=library)

    assert (user_root / "new-skill" / "SKILL.md").read_bytes() == files["SKILL.md"]
    assert (user_root / "new-skill" / "references" / "api.md").exists()
    assert (user_root / "new-skill" / "scripts" / "build.py").exists()
    assert "new-skill" in library.docs


def test_atomic_failure_rolls_back(tmp_path: Path) -> None:
    user_root = tmp_path / "skills"
    user_root.mkdir()
    writer = AtomicSkillWriter(user_root)

    call_count = [0]
    real_replace = __import__("os").replace

    def patched_replace(src: str, dst: str) -> None:
        call_count[0] += 1
        if call_count[0] == 2:
            raise OSError("simulated failure")
        return real_replace(src, dst)

    files = {
        "SKILL.md": b"---\nname: failing\ndescription: X.\n---\n\nBody.\n",
        "references/doc.md": b"Reference content.",
    }
    with patch(
        "octomate.tentacles.agent.skills.atomic.os.replace", side_effect=patched_replace
    ):
        with pytest.raises(OSError, match="simulated failure"):
            writer.commit("failing", files)

    assert not (user_root / "failing").exists()
    # staging dir should be cleaned up
    staging_base = user_root / ".staging"
    remaining = list(staging_base.glob("failing-*")) if staging_base.exists() else []
    assert remaining == []


def test_delete_tombstones_to_trash(skill_root: Path, library: SkillLibrary) -> None:
    writer = AtomicSkillWriter(skill_root)
    writer.delete("test-skill", library=library)
    assert not (skill_root / "test-skill").exists()
    trash = list((skill_root / ".trash").glob("test-skill-*"))
    assert len(trash) == 1
    assert "test-skill" not in library.docs


def test_delete_only_touches_user_root(tmp_path: Path, bundled_root: Path) -> None:
    user_root = tmp_path / "user_skills"
    user_root.mkdir()
    library = SkillLibrary([bundled_root, user_root])
    library.discover()
    writer = AtomicSkillWriter(user_root)

    with pytest.raises(FileNotFoundError):
        writer.delete("bundled-skill", library=library)
    # bundled skill untouched
    assert (bundled_root / "bundled-skill" / "SKILL.md").exists()


def test_patch_bundled_forks_to_user_root(tmp_path: Path, bundled_root: Path) -> None:
    user_root = tmp_path / "user_skills"
    user_root.mkdir()
    library = SkillLibrary([bundled_root, user_root])
    library.discover()
    writer = AtomicSkillWriter(user_root)

    old_content = (bundled_root / "bundled-skill" / "SKILL.md").read_bytes()
    new_content = old_content.replace(b"Bundled content.", b"Forked content.")
    writer.commit(
        "bundled-skill",
        {"SKILL.md": new_content},
        seed_from=bundled_root / "bundled-skill",
        library=library,
    )

    # user root has the override
    assert (
        (user_root / "bundled-skill" / "SKILL.md")
        .read_text()
        .strip()
        .endswith("Forked content.")
    )
    # bundled root unchanged
    assert (bundled_root / "bundled-skill" / "SKILL.md").read_bytes() == old_content


# ── FuzzyMatcher ─────────────────────────────────────────────────────────────


def test_fuzzy_patch_exact_match() -> None:
    text = "Hello World\nFoo bar baz\nEnd"
    m = find_match(text, "Foo bar baz")
    result = apply_patch(text, m, "REPLACED")
    assert result == "Hello World\nREPLACED\nEnd"
    assert m.strategy == "exact"


def test_fuzzy_patch_whitespace_normalized() -> None:
    text = "Line one\n  Foo   bar   baz  \nLine three"
    m = find_match(text, "Foo bar baz")
    result = apply_patch(text, m, "REPLACED")
    assert "REPLACED" in result
    assert m.strategy == "whitespace_normalized"


def test_fuzzy_patch_line_trimmed() -> None:
    # Multi-line old string with different leading indentation — must match
    # via either line_trimmed or whitespace_normalized strategy
    text = "Line one\n    do step one.\n    do step two.\nEnd"
    old = "do step one.\ndo step two."
    m = find_match(text, old)
    result = apply_patch(text, m, "NEW STEPS")
    assert "NEW STEPS" in result
    assert m.strategy in ("line_trimmed", "whitespace_normalized")


def test_fuzzy_patch_ambiguous_rejected() -> None:
    text = "step one\nstep two\nstep one\nstep two"
    with pytest.raises(ValueError, match="ambiguous"):
        find_match(text, "step one\nstep two")


def test_fuzzy_patch_not_found_raises() -> None:
    text = "Completely different content here."
    with pytest.raises(ValueError, match="could not find"):
        find_match(text, "XYZZY NONEXISTENT STRING ABCDEF")


# ── SkillDeduper ─────────────────────────────────────────────────────────────


def test_dedup_exact_name_rejected(library: SkillLibrary) -> None:
    deduper = SkillDeduper(library)
    draft = SkillDraft(name="test-skill", description="Something else.", body="Body.")
    verdict = deduper.check(draft)
    assert verdict.action == "exact_match"
    assert verdict.target == "test-skill"


def test_dedup_name_similar_routes_to_near_duplicate(library: SkillLibrary) -> None:
    deduper = SkillDeduper(library, name_threshold=50)
    draft = SkillDraft(
        name="test-skills",
        description="A test skill for unit testing.",
        body="Does the same thing.",
    )
    verdict = deduper.check(draft)
    assert verdict.action == "near_duplicate"
    assert verdict.target == "test-skill"


def test_dedup_allows_distinct(library: SkillLibrary) -> None:
    deduper = SkillDeduper(library)
    draft = SkillDraft(
        name="deploy-kubernetes",
        description="Deploy services to a Kubernetes cluster using kubectl.",
        body="## Steps\n1. Apply manifests.",
    )
    verdict = deduper.check(draft)
    assert verdict.action == "novel"


def test_dedup_semantic_failure_falls_back_to_stage1(library: SkillLibrary) -> None:
    bad_embedder = MagicMock()
    bad_embedder.embed_candidate.side_effect = RuntimeError("network error")
    deduper = SkillDeduper(library, embedder=bad_embedder)
    draft = SkillDraft(
        name="brand-new", description="Completely unique workflow.", body="Steps."
    )
    verdict = deduper.check(draft)
    # Should not raise; falls back to text-only and returns novel
    assert verdict.action == "novel"


# ── build_skill_evolving_toolset ───────────────────────────────────────────────


def test_evolving_toolset_returns_none_when_evolving_false(tmp_path: Path) -> None:
    library = SkillLibrary([tmp_path])
    library.discover()
    writer = AtomicSkillWriter(tmp_path)
    deduper = SkillDeduper(library)
    cfg = SkillConfig(evolving=False)
    result = build_skill_evolving_toolset(library, writer, deduper, cfg)
    assert result is None


def test_evolving_toolset_returns_toolset_when_evolving_true(tmp_path: Path) -> None:
    from pydantic_ai.toolsets import FunctionToolset

    library = SkillLibrary([tmp_path])
    library.discover()
    writer = AtomicSkillWriter(tmp_path)
    deduper = SkillDeduper(library)
    cfg = SkillConfig(evolving=True)
    result = build_skill_evolving_toolset(library, writer, deduper, cfg)
    assert isinstance(result, FunctionToolset)


# ── SkillDistiller trigger rules ─────────────────────────────────────────────


def _make_state(
    tool_count: int, distinct: set[str], todos_done: bool, error_count: int = 0
):
    from octomate.tentacles.agent.pulse.state import PulseState
    from octomate.transmuters.interactions import Todo

    state = PulseState(prompt="test goal")
    state.tool_call_count = tool_count
    state.distinct_tools_used = distinct
    state.error_count = error_count
    if todos_done:
        state.todos = [
            Todo(todo_id="t1", title="Step 1", description="", status="completed")
        ]
    return state


def _make_distiller(tmp_path: Path):
    from octomate.config import ModelConfig, SkillConfig
    from octomate.tentacles.agent.skills.distiller import SkillDistiller

    user_root = tmp_path / "skills"
    user_root.mkdir()
    library = SkillLibrary([user_root])
    library.discover()
    writer = AtomicSkillWriter(user_root)
    deduper = SkillDeduper(library)
    return SkillDistiller(library, writer, deduper, ModelConfig(), SkillConfig())


def test_distiller_skips_on_short_run(tmp_path: Path) -> None:
    distiller = _make_distiller(tmp_path)
    state = _make_state(tool_count=2, distinct={"bash", "web_search"}, todos_done=False)
    assert not distiller.should_distill(state)


def test_distiller_fires_on_complex_run(tmp_path: Path) -> None:
    distiller = _make_distiller(tmp_path)
    state = _make_state(
        tool_count=6,
        distinct={"bash", "web_search", "web_fetch", "read_file"},
        todos_done=True,
    )
    assert distiller.should_distill(state)


def test_distiller_skips_if_high_error_ratio(tmp_path: Path) -> None:
    distiller = _make_distiller(tmp_path)
    state = _make_state(
        tool_count=6,
        distinct={"bash", "web_search", "web_fetch"},
        todos_done=True,
        error_count=4,
    )
    assert not distiller.should_distill(state)


def test_distiller_skips_if_evolving_tool_used(tmp_path: Path) -> None:
    distiller = _make_distiller(tmp_path)
    state = _make_state(
        tool_count=6,
        distinct={"bash", "web_search", "web_fetch", "create_skill"},
        todos_done=True,
    )
    assert not distiller.should_distill(state)


async def test_distiller_exception_is_silent(tmp_path: Path) -> None:
    distiller = _make_distiller(tmp_path)
    state = _make_state(tool_count=6, distinct={"a", "b", "c"}, todos_done=True)

    async def boom(*args, **kwargs):
        raise RuntimeError("deliberate failure")

    distiller._run = boom  # type: ignore[method-assign]
    # Must not raise
    await distiller.distill([], "goal", state)
