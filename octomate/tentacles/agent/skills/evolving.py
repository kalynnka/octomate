from __future__ import annotations

import json
import logging
import logging.handlers
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from octomate.tentacles.agent.skills.dedup import SkillDeduper, SkillDraft
from octomate.tentacles.agent.skills.fuzzy import apply_patch, find_match
from octomate.tentacles.agent.skills.library import (
    FRONTMATTER_DELIMITER,
    SkillLibrary,
    _parse_skill_file,
)
from octomate.tentacles.agent.skills.writer import AtomicSkillWriter

if TYPE_CHECKING:
    from octomate.config import SkillConfig

logger = logging.getLogger(__name__)

_audit_loggers: dict[str, logging.Logger] = {}


def _get_audit_logger(user_root: Path) -> logging.Logger:
    key = str(user_root)
    if key not in _audit_loggers:
        audit_logger = logging.getLogger(f"octomate.skills.audit.{user_root.name}")
        audit_logger.propagate = False
        audit_logger.setLevel(logging.INFO)
        log_path = Path(tempfile.gettempdir()) / f"octomate-skills-{user_root.name}.log"
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        audit_logger.addHandler(handler)
        _audit_loggers[key] = audit_logger
    return _audit_loggers[key]


def _audit(user_root: Path, entry: dict[str, Any]) -> None:
    entry["ts"] = datetime.now(tz=timezone.utc).isoformat()
    try:
        _get_audit_logger(user_root).info(json.dumps(entry, ensure_ascii=False))
    except Exception as exc:
        logger.warning("skills: audit log write failed: %s", exc)


def _render_skill_md(
    name: str, description: str, body: str, version: str = "1.0.0"
) -> bytes:
    fm = {"name": name, "description": description, "version": version}
    fm_yaml = yaml.dump(fm, allow_unicode=True, default_flow_style=False).strip()
    content = f"{FRONTMATTER_DELIMITER}\n{fm_yaml}\n{FRONTMATTER_DELIMITER}\n\n{body.lstrip()}"
    return content.encode("utf-8")


def _validate_frontmatter_bytes(content: bytes) -> str | None:
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
        f.write(content)
        tmp = Path(f.name)
    try:
        fm, body = _parse_skill_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    if not fm.get("name"):
        return "frontmatter missing 'name'"
    if not fm.get("description"):
        return "frontmatter missing 'description'"
    if not body.strip():
        return "skill body is empty"
    return None


def _is_under_user_root(skill_path: Path, user_root: Path) -> bool:
    try:
        skill_path.relative_to(user_root)
        return True
    except ValueError:
        return False


def build_skill_evolving_toolset(
    library: SkillLibrary,
    writer: AtomicSkillWriter,
    deduper: SkillDeduper,
    config: SkillConfig,
) -> FunctionToolset[Any] | None:
    if not config.evolving:
        return None

    user_root = writer.user_root
    toolset: FunctionToolset[Any] = FunctionToolset()

    @toolset.tool(requires_approval=False)
    async def create_skill(
        ctx: RunContext[Any],
        name: str,
        description: str,
        body: str,
        references: dict[str, str] | None = None,
        scripts: dict[str, str] | None = None,
        _force: bool = False,
    ) -> str:
        """Create a new reusable skill under the user's skill directory.

        Use after completing a complex workflow that is worth capturing for
        future reuse.  The skill will appear in list_skills() immediately.

        Only creates skills in the user's workdir — never touches bundled skills.
        If a near-duplicate already exists, you will be told which skill to patch
        instead.  Pass _force=True to override dedup and create anyway.

        Args:
            name: Kebab-case identifier (e.g. "summarize-urls"). Max 64 chars.
            description: One-sentence description including WHEN to use this skill.
                         This is the primary triggering signal — make it specific.
            body: Full markdown instructions.  Include numbered steps, pitfalls,
                  and a verification section.
            references: Optional dict of filename→content for references/ files.
            scripts: Optional dict of filename→content for scripts/ files.
            _force: Skip dedup check and create even if a near-duplicate exists.
        """
        if len(body) > config.max_skill_body_chars:
            return (
                f"body too long: {len(body)} chars (max {config.max_skill_body_chars})"
            )

        if not _force:
            draft = SkillDraft(name=name, description=description, body=body)
            verdict = deduper.check(draft)
            if verdict.action == "exact_match":
                return (
                    f"Skill '{verdict.target}' already exists with this name. "
                    f"Use patch_skill or append_skill_section to update it."
                )
            if verdict.action == "near_duplicate":
                existing = library.docs.get(verdict.target or "")
                snippet = (existing.body[:200] + "…") if existing else ""
                return (
                    f"Near-duplicate detected ({verdict.reason}). "
                    f"Existing skill '{verdict.target}':\n{snippet}\n\n"
                    f"Call patch_skill('{verdict.target}', ...) to update it, "
                    f"or pass _force=True to create anyway."
                )

        files: dict[str, bytes | None] = {
            "SKILL.md": _render_skill_md(name, description, body),
        }
        for fname, content in (references or {}).items():
            files[f"references/{fname}"] = content.encode("utf-8")
        for fname, content in (scripts or {}).items():
            files[f"scripts/{fname}"] = content.encode("utf-8")

        try:
            writer.commit(name, files, library=library)
        except (ValueError, OSError) as exc:
            return f"create_skill failed: {exc}"

        _audit(
            user_root,
            {"action": "create", "name": name, "actor": "agent", "files": list(files)},
        )
        return f"Created skill '{name}'. It is now available via list_skills()."

    @toolset.tool(requires_approval=False)
    async def patch_skill(
        ctx: RunContext[Any],
        name: str,
        old_string: str,
        new_string: str,
        target: str = "SKILL.md",
    ) -> str:
        """Replace a string in a skill file using fuzzy matching.

        Use to fix incorrect, outdated, or incomplete content in a skill you
        just loaded.  Prefer small surgical edits over full rewrites.

        If the skill is in octoskills/ (bundled), this creates an override copy
        in your workdir automatically.

        Args:
            name: The skill name as returned by list_skills().
            old_string: The text to replace (may have minor whitespace differences).
            new_string: The replacement text.
            target: Which file to patch — defaults to SKILL.md.  Use
                    "references/<filename>" to patch a reference file.
        """
        doc = library.docs.get(name)
        if doc is None:
            return f"Skill '{name}' not found."

        target_path = doc.skill_dir / target
        if not target_path.exists():
            return f"File '{target}' not found in skill '{name}'."

        original = target_path.read_text(encoding="utf-8")
        try:
            match = find_match(
                original, old_string, threshold=config.fuzzy_patch_threshold
            )
        except ValueError as exc:
            return str(exc)

        new_content = apply_patch(original, match, new_string)

        if target == "SKILL.md":
            err = _validate_frontmatter_bytes(new_content.encode("utf-8"))
            if err:
                return f"patch would break SKILL.md structure: {err}"

        seed_from = (
            doc.skill_dir if not _is_under_user_root(doc.skill_dir, user_root) else None
        )
        files: dict[str, bytes | None] = {target: new_content.encode("utf-8")}
        try:
            writer.commit(name, files, seed_from=seed_from, library=library)
        except (ValueError, OSError) as exc:
            return f"patch_skill failed: {exc}"

        _audit(
            user_root,
            {
                "action": "patch",
                "name": name,
                "target": target,
                "actor": "agent",
                "strategy": match.strategy,
                **({"forked_from": str(doc.skill_dir)} if seed_from else {}),
            },
        )
        return f"Patched '{target}' in skill '{name}' (strategy: {match.strategy})."

    @toolset.tool(requires_approval=False)
    async def append_skill_section(
        ctx: RunContext[Any],
        name: str,
        heading: str,
        content: str,
    ) -> str:
        """Append a new section to a skill's SKILL.md body.

        Safer than patch_skill for pure additions — no fuzzy matching needed.
        Use to add missing steps, pitfalls, or notes discovered during use.

        Args:
            name: The skill name.
            heading: The section heading text (e.g. "Pitfall: Alpine numpy").
            content: The markdown content to append under the heading.
        """
        doc = library.docs.get(name)
        if doc is None:
            return f"Skill '{name}' not found."

        skill_md = doc.skill_dir / "SKILL.md"
        original = skill_md.read_text(encoding="utf-8")
        section = f"\n\n## {heading}\n\n{content.strip()}\n"
        new_content = original.rstrip() + section

        seed_from = (
            doc.skill_dir if not _is_under_user_root(doc.skill_dir, user_root) else None
        )
        files: dict[str, bytes | None] = {"SKILL.md": new_content.encode("utf-8")}
        try:
            writer.commit(name, files, seed_from=seed_from, library=library)
        except (ValueError, OSError) as exc:
            return f"append_skill_section failed: {exc}"

        _audit(
            user_root,
            {
                "action": "append_section",
                "name": name,
                "heading": heading,
                "actor": "agent",
            },
        )
        return f"Appended section '{heading}' to skill '{name}'."

    @toolset.tool(requires_approval=False)
    async def add_skill_reference(
        ctx: RunContext[Any],
        name: str,
        ref_name: str,
        content: str,
    ) -> str:
        """Add or replace a file in a skill's references/ directory.

        Use for supplementary documentation, schemas, or domain specs that are
        too large to inline in SKILL.md.

        Args:
            name: The skill name.
            ref_name: Filename within references/ (e.g. "api-spec.md").
            content: The text content of the reference file.
        """
        doc = library.docs.get(name)
        if doc is None:
            return f"Skill '{name}' not found."

        seed_from = (
            doc.skill_dir if not _is_under_user_root(doc.skill_dir, user_root) else None
        )
        files: dict[str, bytes | None] = {
            f"references/{ref_name}": content.encode("utf-8")
        }
        try:
            writer.commit(name, files, seed_from=seed_from, library=library)
        except (ValueError, OSError) as exc:
            return f"add_skill_reference failed: {exc}"

        _audit(
            user_root,
            {
                "action": "add_reference",
                "name": name,
                "ref_name": ref_name,
                "actor": "agent",
            },
        )
        return f"Added reference '{ref_name}' to skill '{name}'."

    @toolset.tool(requires_approval=False)
    async def delete_skill(
        ctx: RunContext[Any],
        name: str,
    ) -> str:
        """Delete a skill from the user's workdir (moves it to trash, recoverable).

        Only deletes skills in the user's workdir — bundled octoskills/ entries
        cannot be deleted this way.

        Args:
            name: The skill name to delete.
        """
        doc = library.docs.get(name)
        if doc is None:
            return f"Skill '{name}' not found."
        if not _is_under_user_root(doc.skill_dir, user_root):
            return (
                f"Skill '{name}' is a bundled skill in {doc.skill_dir} and cannot be "
                f"deleted via this tool. Only user-created skills in the workdir can be deleted."
            )
        try:
            writer.delete(name, library=library)
        except (ValueError, FileNotFoundError, OSError) as exc:
            return f"delete_skill failed: {exc}"

        _audit(user_root, {"action": "delete", "name": name, "actor": "agent"})
        return f"Deleted skill '{name}' (moved to trash, recoverable)."

    return toolset
