from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset

from octomate.tentacles.agent.skills import SkillDoc, SkillLibrary

SKILL_MD = textwrap.dedent("""\
    ---
    name: test-skill
    description: A test skill for unit testing.
    version: 1.0.0
    ---

    # Test Skill

    Do the thing when asked.
""")

MISSING_NAME_MD = textwrap.dedent("""\
    ---
    description: No name field here.
    ---

    Body.
""")

NO_FRONTMATTER_MD = "# Just a header\n\nSome content.\n"


@pytest.fixture
def skill_root(tmp_path: Path) -> Path:
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL_MD)
    return tmp_path


def test_discover_finds_valid_skill(skill_root: Path) -> None:
    lib = SkillLibrary([skill_root])
    lib.discover()
    assert "test-skill" in lib.docs


def test_discover_populates_fields(skill_root: Path) -> None:
    lib = SkillLibrary([skill_root])
    lib.discover()
    doc = lib.docs["test-skill"]
    assert isinstance(doc, SkillDoc)
    assert doc.name == "test-skill"
    assert doc.description == "A test skill for unit testing."
    assert "Do the thing when asked." in doc.body


def test_discover_skips_missing_name(tmp_path: Path) -> None:
    d = tmp_path / "bad-skill"
    d.mkdir()
    (d / "SKILL.md").write_text(MISSING_NAME_MD)
    lib = SkillLibrary([tmp_path])
    lib.discover()
    assert len(lib.docs) == 0


def test_discover_skips_no_frontmatter(tmp_path: Path) -> None:
    d = tmp_path / "plain-skill"
    d.mkdir()
    (d / "SKILL.md").write_text(NO_FRONTMATTER_MD)
    lib = SkillLibrary([tmp_path])
    lib.discover()
    assert len(lib.docs) == 0


def test_discover_nonexistent_root_skipped(tmp_path: Path) -> None:
    lib = SkillLibrary([tmp_path / "does-not-exist"])
    lib.discover()
    assert len(lib.docs) == 0


def test_later_root_overrides_earlier(tmp_path: Path) -> None:
    root1 = tmp_path / "root1"
    root2 = tmp_path / "root2"
    for root in (root1, root2):
        d = root / "my-skill"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: my-skill\ndescription: From {root.name}.\n---\n\nBody from {root.name}.\n"
        )
    lib = SkillLibrary([root1, root2])
    lib.discover()
    assert lib.docs["my-skill"].body.strip() == f"Body from {root2.name}."


def test_load_returns_body(skill_root: Path) -> None:
    lib = SkillLibrary([skill_root])
    lib.discover()
    body = lib.load("test-skill")
    assert "Do the thing when asked." in body


def test_load_unknown_raises(skill_root: Path) -> None:
    lib = SkillLibrary([skill_root])
    lib.discover()
    with pytest.raises(KeyError, match="no-such-skill"):
        lib.load("no-such-skill")


def test_to_toolset_returns_function_toolset(skill_root: Path) -> None:
    lib = SkillLibrary([skill_root])
    lib.discover()
    assert isinstance(lib.toolset, FunctionToolset)


async def test_list_skills_tool(skill_root: Path) -> None:
    lib = SkillLibrary([skill_root])
    lib.discover()

    agent = Agent(TestModel(), output_type=str, toolsets=[lib.toolset])
    result = await agent.run("list skills")
    assert result is not None


async def test_load_skill_tool_via_agent(skill_root: Path) -> None:
    lib = SkillLibrary([skill_root])
    lib.discover()

    agent = Agent(TestModel(), output_type=str, toolsets=[lib.toolset])
    result = await agent.run("load test-skill")
    assert result is not None
