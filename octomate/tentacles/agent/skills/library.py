from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

logger = logging.getLogger(__name__)

SKILL_METADATA_KEY = "skill"
FRONTMATTER_DELIMITER = "---"

_SCRIPT_TIMEOUT = 60  # seconds


def _parse_skill_file(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        return {}, text

    end = next(
        (
            i
            for i, ln in enumerate(lines[1:], start=1)
            if ln.strip() == FRONTMATTER_DELIMITER
        ),
        None,
    )
    if end is None:
        return {}, text

    fm_text = "".join(lines[1:end])
    body = "".join(lines[end + 1 :]).lstrip("\n")
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        logger.warning("skills: failed to parse frontmatter in %s", path)
        fm = {}
    return fm, body


@dataclass
class SkillDoc:
    name: str
    description: str
    path: Path
    frontmatter: dict[str, Any]
    body: str
    references: dict[str, Path] = field(default_factory=dict)
    scripts: dict[str, Path] = field(default_factory=dict)
    assets: dict[str, Path] = field(default_factory=dict)

    @property
    def skill_dir(self) -> Path:
        return self.path.parent


def _scan_dir(skill_dir: Path, subdir: str) -> dict[str, Path]:
    d = skill_dir / subdir
    if not d.is_dir():
        return {}
    return {p.name: p for p in sorted(d.iterdir()) if p.is_file()}


class SkillLibrary:
    docs: dict[str, SkillDoc]
    roots: list[Path]

    def __init__(self, roots: Sequence[Path | str]) -> None:
        self.roots = [Path(r) for r in roots]
        self.docs = {}

    def scan(self) -> None:
        """Populate self.docs from all roots (does not clear first).

        Later roots override earlier ones on name clash, so user skills
        in .octomate/skills/ naturally override built-ins in octoskills/.
        """
        for root in self.roots:
            if not root.is_dir():
                continue
            for skill_md in sorted(root.glob("*/SKILL.md")):
                fm, body = _parse_skill_file(skill_md)
                name = fm.get("name", "")
                description = fm.get("description", "")
                if not name or not description:
                    logger.warning(
                        "skills: skipping %s — missing name or description in frontmatter",
                        skill_md,
                    )
                    continue
                skill_dir = skill_md.parent
                self.docs[name] = SkillDoc(
                    name=name,
                    description=description,
                    path=skill_md,
                    frontmatter=fm,
                    body=body,
                    references=_scan_dir(skill_dir, "references"),
                    scripts=_scan_dir(skill_dir, "scripts"),
                    assets=_scan_dir(skill_dir, "assets"),
                )
                logger.debug("skills: registered skill %r from %s", name, skill_md)

    def discover(self) -> None:
        self.scan()
        logger.info("skills: discovered %d skill(s)", len(self.docs))

    def reload(self) -> None:
        """Clear and re-scan all roots. Safe to call at any time; the toolset
        closure holds a reference to self.docs so callers see changes immediately.
        """
        old_count = len(self.docs)
        self.docs.clear()
        self.scan()
        logger.info("skills: reloaded %d skill(s) (was %d)", len(self.docs), old_count)

    async def watch(self) -> None:
        """Watch all skill roots and reload whenever any file changes."""
        import watchfiles

        watch_paths = [str(r) for r in self.roots if r.is_dir()]
        if not watch_paths:
            return
        logger.info("skills: watching %s", ", ".join(watch_paths))
        async for changes in watchfiles.awatch(*watch_paths):
            logger.info("skills: %d file(s) changed, reloading", len(changes))
            self.reload()

    def load(self, name: str) -> str:
        doc = self.docs.get(name)
        if doc is None:
            raise KeyError(f"skill {name!r} not found")
        return doc.body

    def find_in_any_root(self, name: str) -> Path | None:
        """Search all roots for a skill directory by name, not just loaded docs."""
        for root in self.roots:
            candidate = root / name
            if (candidate / "SKILL.md").exists():
                return candidate
        return None

    @property
    def toolset(self) -> FunctionToolset[Any]:
        return _build_read_toolset(self.docs)


def _build_read_toolset(docs: dict[str, SkillDoc]) -> FunctionToolset[Any]:
    toolset: FunctionToolset[Any] = FunctionToolset()

    @toolset.tool(requires_approval=False)
    async def list_skills(ctx: RunContext[Any]) -> list[dict[str, Any]]:
        """List all available guidance skills.

        Returns name, description, and available bundled resources for each
        skill. Call load_skill(name) to read instructions. Call
        load_reference(skill_name, ref_name) to read a bundled reference
        file. Call run_script(skill_name, script_name) to execute a bundled
        script.
        """
        result = []
        for d in docs.values():
            entry: dict[str, Any] = {"name": d.name, "description": d.description}
            if d.references:
                entry["references"] = sorted(d.references)
            if d.scripts:
                entry["scripts"] = sorted(d.scripts)
            if d.assets:
                entry["assets"] = sorted(d.assets)
            result.append(entry)
        return result

    @toolset.tool(requires_approval=False)
    async def load_skill(ctx: RunContext[Any], name: str) -> str:
        """Load the full instructions for a named guidance skill.

        Returns the skill's markdown body — read it, then follow its
        instructions to complete the user's request.

        Args:
            name: The skill name as returned by list_skills().
        """
        doc = docs.get(name)
        if doc is None:
            known = ", ".join(sorted(docs)) or "(none)"
            return f"Skill {name!r} not found. Available skills: {known}"
        return doc.body

    @toolset.tool(requires_approval=False)
    async def load_reference(
        ctx: RunContext[Any],
        skill_name: str,
        reference_name: str,
    ) -> str:
        """Load a bundled reference file from a skill's references/ directory.

        Use when a skill's instructions point you to a reference file for
        additional detail (schemas, domain docs, format specs, etc.).

        Args:
            skill_name: The skill name as returned by list_skills().
            reference_name: Filename as listed in the skill's references list.
        """
        doc = docs.get(skill_name)
        if doc is None:
            return f"Skill {skill_name!r} not found."
        ref_path = doc.references.get(reference_name)
        if ref_path is None:
            known = ", ".join(sorted(doc.references)) or "(none)"
            return f"Reference {reference_name!r} not found in skill {skill_name!r}. Available: {known}"
        return ref_path.read_text(encoding="utf-8")

    @toolset.tool(requires_approval=False)
    async def run_script(
        ctx: RunContext[Any],
        skill_name: str,
        script_name: str,
        args: list[str] | None = None,
    ) -> str:
        """Run a bundled script from a skill's scripts/ directory.

        Scripts handle deterministic, repetitive work — file transforms,
        aggregations, code generation — so you don't have to reinvent them
        each run. Returns combined stdout + stderr. Runs with a 60-second
        timeout.

        Args:
            skill_name: The skill name as returned by list_skills().
            script_name: Filename as listed in the skill's scripts list.
            args: Optional list of command-line arguments to pass.
        """
        doc = docs.get(skill_name)
        if doc is None:
            return f"Skill {skill_name!r} not found."
        script_path = doc.scripts.get(script_name)
        if script_path is None:
            known = ", ".join(sorted(doc.scripts)) or "(none)"
            return f"Script {script_name!r} not found in skill {skill_name!r}. Available: {known}"

        cmd = ["python", str(script_path), *(args or [])]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(doc.skill_dir),
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=_SCRIPT_TIMEOUT,
            )
            output = stdout.decode(errors="replace").strip()
            return (
                output
                if output
                else f"(script exited with code {proc.returncode}, no output)"
            )
        except TimeoutError:
            proc.kill()
            return f"Script timed out after {_SCRIPT_TIMEOUT}s."
        except Exception as e:
            return f"Failed to run script: {e}"

    return toolset
