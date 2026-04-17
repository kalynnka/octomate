from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from octomate.tentacles.agent.skills.library import (
    FRONTMATTER_DELIMITER,
    SKILL_METADATA_KEY,
    SkillDoc,
    SkillLibrary,
    _parse_skill_file,
    _scan_dir,
)
from octomate.tentacles.agent.skills.manager import (
    SkillInfo,
    SkillManager,
    ToolPermission,
    make_metadata_injector,
)

if TYPE_CHECKING:
    from octomate.config import PulseAgentConfig
    from octomate.tentacles.agent.skills.distiller import SkillDistiller

__all__ = [
    "FRONTMATTER_DELIMITER",
    "SKILL_METADATA_KEY",
    "SkillDoc",
    "SkillLibrary",
    "SkillInfo",
    "SkillManager",
    "ToolPermission",
    "make_metadata_injector",
    "_parse_skill_file",
    "_scan_dir",
    "build_skill_manager",
]

logger = logging.getLogger(__name__)


def build_skill_manager(
    pulse_cfg: PulseAgentConfig,
    workdir: Path,
) -> tuple[SkillManager, SkillDistiller | None]:
    # Lazy imports to avoid circular dependencies at module load time.
    from octomate.tentacles.agent.skills.dedup import GeminiEmbedder, SkillDeduper
    from octomate.tentacles.agent.skills.distiller import SkillDistiller
    from octomate.tentacles.agent.skills.evolving import build_skill_evolving_toolset
    from octomate.tentacles.agent.skills.writer import AtomicSkillWriter

    skill_cfg = pulse_cfg.skill
    user_root = workdir / skill_cfg.user_root_name

    skill_roots = list(pulse_cfg.skill_roots)
    if user_root not in [Path(r) for r in skill_roots]:
        skill_roots.append(user_root)

    library = SkillLibrary(skill_roots)
    library.discover()

    manager = SkillManager()
    if library.docs:
        manager.register_skill_library("skills", library)

    distiller: SkillDistiller | None = None
    if skill_cfg.evolving:
        writer = AtomicSkillWriter(user_root)
        embedder: GeminiEmbedder | None = None
        if skill_cfg.semantic_dedup_enabled and pulse_cfg.main.api_key:
            try:
                embedder = GeminiEmbedder(
                    api_key=pulse_cfg.main.api_key,
                    model=skill_cfg.embedding_model,
                    task_type=skill_cfg.embedding_task_type,
                )
            except Exception as exc:
                logger.warning("skills: failed to init Gemini embedder: %s", exc)
        deduper = SkillDeduper(
            library,
            name_threshold=skill_cfg.dedup_name_threshold,
            semantic_threshold=skill_cfg.dedup_semantic_threshold,
            embedder=embedder,
        )
        evolving_toolset = build_skill_evolving_toolset(library, writer, deduper, skill_cfg)
        if evolving_toolset is not None:
            manager.register_toolset(
                "skill-evolving",
                "Create, patch, append, and delete self-managed skills in the user workdir.",
                evolving_toolset,
            )
        distiller = SkillDistiller(library, writer, deduper, pulse_cfg.main, skill_cfg)

    return manager, distiller
