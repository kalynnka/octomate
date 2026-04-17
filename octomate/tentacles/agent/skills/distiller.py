from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

import httpx
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.settings import ModelSettings

from octomate.tentacles.agent.skills.dedup import SkillDeduper, SkillDraft
from octomate.tentacles.agent.skills.evolving import (
    _audit,
    _is_under_user_root,
    _render_skill_md,
    _validate_frontmatter_bytes,
)
from octomate.tentacles.agent.skills.fuzzy import apply_patch, find_match
from octomate.tentacles.agent.skills.library import FRONTMATTER_DELIMITER
from octomate.tentacles.agent.skills.writer import AtomicSkillWriter

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

    from octomate.config import ModelConfig, SkillConfig
    from octomate.tentacles.agent.pulse.state import PulseState
    from octomate.tentacles.agent.skills.library import SkillLibrary

logger = logging.getLogger(__name__)


class DistillOutput(BaseModel):
    action: Literal["create", "skip"]
    reason: str
    name: str | None = None
    description: str | None = None
    body: str | None = None


class PatchDraft(BaseModel):
    target: str
    old_string: str
    new_string: str


_DISTILL_SYSTEM = """\
You are a skill extractor.  Given a transcript of a successful multi-step \
agent task, decide whether the workflow is reusable enough to capture as a \
skill.

If yes, output action="create" with:
- name: kebab-case identifier (max 64 chars, lowercase, dashes only)
- description: one sentence describing WHEN to trigger this skill and what it \
does.  Be specific about trigger conditions.
- body: markdown instructions including numbered steps, pitfalls encountered, \
and a verification section.  Omit user-specific details.

If the workflow is one-off, trivial, or already well-known, output action="skip" \
with a brief reason.
"""

_PATCH_SYSTEM = """\
You are a skill patcher.  Given an existing skill body and a new workflow \
transcript, produce a minimal patch to add the new information.

Output the exact old_string to replace and the new_string to replace it with.
If nothing needs changing, output old_string="" and new_string="".
Keep changes small.  Only add new pitfalls, steps, or notes that are genuinely \
missing.
"""


def _build_model(config: ModelConfig) -> GoogleModel:
    kwargs: dict[str, Any] = {}
    if config.vertexai:
        kwargs["vertexai"] = True
        if config.project:
            kwargs["project"] = config.project
        if config.location:
            kwargs["location"] = config.location
    elif config.api_key:
        kwargs["api_key"] = config.api_key
    return GoogleModel(
        config.model,
        provider=GoogleProvider(http_client=httpx.AsyncClient(timeout=60.0), **kwargs),
    )


def _summarise_transcript(messages: list[ModelMessage], goal: str) -> str:
    lines = [f"Goal: {goal}", ""]
    for msg in messages:
        for part in msg.parts:
            if isinstance(part, ToolCallPart):
                args_str = str(part.args)[:200]
                lines.append(f"[tool] {part.tool_name}({args_str})")
            elif isinstance(part, ToolReturnPart):
                lines.append(f"[result] {str(part.content)[:200]}")
    return "\n".join(lines)[:8000]


def _count_tool_calls(messages: list[ModelMessage]) -> tuple[int, set[str], int]:
    """Return (total_calls, distinct_tool_names, retry_count)."""
    total = 0
    names: set[str] = set()
    retries = 0
    for msg in messages:
        for part in msg.parts:
            if isinstance(part, ToolCallPart):
                total += 1
                names.add(part.tool_name)
            elif isinstance(part, RetryPromptPart):
                retries += 1
    return total, names, retries


class SkillDistiller:
    """Background task that auto-creates or patches skills from completed runs."""

    def __init__(
        self,
        library: SkillLibrary,
        writer: AtomicSkillWriter,
        deduper: SkillDeduper,
        model_config: ModelConfig,
        skill_config: SkillConfig,
    ) -> None:
        self.library = library
        self.writer = writer
        self.deduper = deduper
        self.model_config = model_config
        self.skill_config = skill_config
        self._distill_agent: Agent[None, DistillOutput] | None = None
        self._patch_agent: Agent[None, PatchDraft] | None = None

    def _build_distill_system_prompt(self) -> str:
        doc = self.library.docs.get("create-skill")
        if doc is None:
            return _DISTILL_SYSTEM
        return _DISTILL_SYSTEM + f"\n\nSkill writing conventions to follow:\n{doc.body}"

    def _get_distill_agent(self) -> Agent[None, DistillOutput]:
        if self._distill_agent is None:
            model = _build_model(self.model_config)
            settings = (
                ModelSettings(thinking="low") if self.model_config.thinking else None
            )
            self._distill_agent = Agent(
                model,
                system_prompt=self._build_distill_system_prompt(),
                output_type=DistillOutput,
                model_settings=settings,
            )
        return self._distill_agent

    def _get_patch_agent(self) -> Agent[None, PatchDraft]:
        if self._patch_agent is None:
            model = _build_model(self.model_config)
            settings = (
                ModelSettings(thinking="low") if self.model_config.thinking else None
            )
            self._patch_agent = Agent(
                model,
                system_prompt=_PATCH_SYSTEM,
                output_type=PatchDraft,
                model_settings=settings,
            )
        return self._patch_agent

    def should_distill(self, state: PulseState) -> bool:
        cfg = self.skill_config
        tool_count, distinct, retries = _count_tool_calls(
            getattr(state, "_messages", []) or []
        )
        tool_count = getattr(state, "tool_call_count", tool_count)
        distinct = getattr(state, "distinct_tools_used", distinct)
        retries = getattr(state, "error_count", retries)

        if tool_count < cfg.min_tool_calls:
            return False
        if len(distinct) < cfg.min_distinct_tools:
            return False
        if not any(t.status == "completed" for t in (state.todos or [])):
            return False
        if tool_count > 0 and (retries / tool_count) >= 0.5:
            return False
        evolving_tools = {
            "create_skill",
            "patch_skill",
            "append_skill_section",
            "add_skill_reference",
            "delete_skill",
        }
        if distinct & evolving_tools:
            return False
        return True

    async def distill(
        self,
        messages: list[ModelMessage],
        goal: str,
        state: PulseState,
    ) -> None:
        """Run distillation in the background; swallows all exceptions."""
        try:
            await self._run(messages, goal, state)
        except Exception as exc:
            logger.warning("skills: distillation failed (non-fatal): %s", exc)

    async def _run(
        self,
        messages: list[ModelMessage],
        goal: str,
        state: PulseState,
    ) -> None:
        transcript = _summarise_transcript(messages, goal)
        agent = self._get_distill_agent()
        result = await agent.run(transcript)
        output = result.output

        if output.action == "skip":
            logger.debug("skills: distiller decided skip: %s", output.reason)
            return

        if not output.name or not output.description or not output.body:
            logger.warning("skills: distiller produced incomplete output, skipping")
            return

        draft = SkillDraft(
            name=output.name,
            description=output.description,
            body=output.body,
        )
        verdict = self.deduper.check(draft)

        if verdict.action == "exact_match":
            logger.debug(
                "skills: distiller skipping exact-match duplicate: %s", verdict.target
            )
            return

        if verdict.action == "novel":
            files = {
                "SKILL.md": _render_skill_md(
                    output.name, output.description, output.body
                )
            }
            self.writer.commit(output.name, files, library=self.library)
            _audit(
                self.writer.user_root,
                {
                    "action": "auto_create",
                    "name": output.name,
                    "actor": "distiller",
                    "files": list(files),
                },
            )
            logger.info("skills: auto-created skill '%s'", output.name)
            return

        assert verdict.target is not None
        existing_doc = self.library.docs.get(verdict.target)
        if existing_doc is None:
            logger.warning(
                "skills: near-dup target '%s' not in library, skipping", verdict.target
            )
            return

        existing_body = existing_doc.body
        patch_prompt = (
            f"Existing skill body:\n{existing_body}\n\n"
            f"New workflow transcript:\n{transcript}\n\n"
            f"Dedup reason: {verdict.reason}"
        )
        patch_agent = self._get_patch_agent()
        patch_result = await patch_agent.run(patch_prompt)
        patch = patch_result.output

        if not patch.old_string:
            logger.debug(
                "skills: patch agent decided no changes needed for '%s'", verdict.target
            )
            return

        try:
            match = find_match(
                existing_body,
                patch.old_string,
                threshold=self.skill_config.fuzzy_patch_threshold,
            )
        except ValueError as exc:
            logger.warning(
                "skills: patch fuzzy match failed for '%s': %s", verdict.target, exc
            )
            return

        new_body = apply_patch(existing_body, match, patch.new_string)
        skill_md_content = existing_doc.path.read_bytes()
        fm_text = skill_md_content.decode()
        parts = fm_text.split(f"{FRONTMATTER_DELIMITER}\n", 2)
        if len(parts) >= 3:
            new_skill_md = f"{FRONTMATTER_DELIMITER}\n{parts[1]}{FRONTMATTER_DELIMITER}\n\n{new_body.lstrip()}"
        else:
            new_skill_md = fm_text

        err = _validate_frontmatter_bytes(new_skill_md.encode())
        if err:
            logger.warning(
                "skills: distiller patch would break frontmatter of '%s': %s",
                verdict.target,
                err,
            )
            return

        seed_from = (
            existing_doc.skill_dir
            if not _is_under_user_root(existing_doc.skill_dir, self.writer.user_root)
            else None
        )
        self.writer.commit(
            verdict.target,
            {"SKILL.md": new_skill_md.encode()},
            seed_from=seed_from,
            library=self.library,
        )
        _audit(
            self.writer.user_root,
            {
                "action": "auto_patch",
                "name": verdict.target,
                "actor": "distiller",
                "strategy": match.strategy,
            },
        )
        logger.info("skills: auto-patched skill '%s'", verdict.target)
