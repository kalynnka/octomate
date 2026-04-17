from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rapidfuzz import fuzz

if TYPE_CHECKING:
    from octomate.tentacles.agent.skills.library import SkillLibrary

logger = logging.getLogger(__name__)


@dataclass
class SkillDraft:
    name: str
    description: str
    body: str


@dataclass
class DedupVerdict:
    action: str  # "novel" | "near_duplicate" | "exact_match"
    target: str | None = None
    score: float = 0.0
    reason: str = ""


class GeminiEmbedder:
    """Thin wrapper around google-genai embedding with an mtime-keyed in-memory cache."""

    def __init__(self, api_key: str, model: str, task_type: str) -> None:
        import google.genai as genai  # lazy import

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._task_type = task_type
        self._cache: dict[str, tuple[float, list[float]]] = {}

    def _embed(self, text: str) -> list[float]:
        result = self._client.models.embed_content(
            model=self._model,
            contents=text,
            config={"task_type": self._task_type},
        )
        if not result.embeddings:
            raise ValueError("embedding returned no embeddings")
        values = result.embeddings[0].values
        if not values:
            raise ValueError("embedding returned empty values")
        return values

    def embed_candidate(self, draft: SkillDraft) -> list[float]:
        text = f"{draft.name}\n{draft.description}\n\n{draft.body[:2000]}"
        return self._embed(text)

    def embed_skill(self, name: str, mtime: float, description: str, body: str) -> list[float]:
        cached = self._cache.get(name)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        text = f"{name}\n{description}\n\n{body[:2000]}"
        vec = self._embed(text)
        self._cache[name] = (mtime, vec)
        return vec


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _slugify(name: str) -> str:
    return name.lower().replace(" ", "-")


class SkillDeduper:
    """Two-stage duplicate detector: rapidfuzz text (always) + Gemini embeddings (optional)."""

    def __init__(
        self,
        library: SkillLibrary,
        name_threshold: int = 80,
        semantic_threshold: float = 0.88,
        embedder: GeminiEmbedder | None = None,
    ) -> None:
        self.library = library
        self.name_threshold = name_threshold
        self.semantic_threshold = semantic_threshold
        self.embedder = embedder

    def check(self, candidate: SkillDraft) -> DedupVerdict:
        slug = _slugify(candidate.name)

        # Hard exact-name check (case-insensitive, kebab-slugged)
        for existing in self.library.docs.values():
            if _slugify(existing.name) == slug:
                return DedupVerdict(
                    action="exact_match",
                    target=existing.name,
                    score=100.0,
                    reason=f"Skill '{existing.name}' already exists with the same name.",
                )

        cand_text = f"{candidate.name} {candidate.description}"

        best_score = 0.0
        best_name: str | None = None

        # Stage 1: rapidfuzz token_set_ratio on name+description
        for existing in self.library.docs.values():
            existing_text = f"{existing.name} {existing.description}"
            score = fuzz.token_set_ratio(cand_text, existing_text)
            if score > best_score:
                best_score = score
                best_name = existing.name

        if best_score >= self.name_threshold and best_name is not None:
            return DedupVerdict(
                action="near_duplicate",
                target=best_name,
                score=best_score,
                reason=f"Name/description similarity {best_score:.0f}% with '{best_name}'.",
            )

        # Stage 2 (optional): Gemini embedding cosine similarity
        if self.embedder is not None:
            try:
                cand_vec = self.embedder.embed_candidate(candidate)
                best_cos = 0.0
                best_cos_name: str | None = None
                for existing in self.library.docs.values():
                    mtime = existing.path.stat().st_mtime
                    ex_vec = self.embedder.embed_skill(
                        existing.name, mtime, existing.description, existing.body
                    )
                    cos = _cosine(cand_vec, ex_vec)
                    if cos > best_cos:
                        best_cos = cos
                        best_cos_name = existing.name
                if best_cos >= self.semantic_threshold and best_cos_name is not None:
                    return DedupVerdict(
                        action="near_duplicate",
                        target=best_cos_name,
                        score=best_cos,
                        reason=(
                            f"Semantic similarity {best_cos:.2f} with '{best_cos_name}' "
                            f"(threshold {self.semantic_threshold})."
                        ),
                    )
            except Exception as exc:
                logger.warning("skills: embedding dedup failed, falling back to text-only: %s", exc)

        return DedupVerdict(action="novel")
