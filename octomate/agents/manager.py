from __future__ import annotations

from dataclasses import dataclass
from typing import Any, runtime_checkable

from pydantic_ai import RunContext, ToolDefinition
from pydantic_ai.toolsets import (
    AbstractToolset,
    CombinedToolset,
    FilteredToolset,
    FunctionToolset,
)
from typing_extensions import Protocol

SKILL_METADATA_KEY = "skill"


@runtime_checkable
class SkillDeps(Protocol):
    active_skills: set[str]


def skill_filter(ctx: RunContext[Any], tool_def: ToolDefinition) -> bool:
    skill_name = (tool_def.metadata or {}).get(SKILL_METADATA_KEY)
    if skill_name is None:
        return True
    deps = ctx.deps
    if not isinstance(deps, SkillDeps):
        return True
    return skill_name in deps.active_skills


@dataclass
class SkillInfo:
    name: str
    description: str


class SkillManager:
    _skills: dict[str, SkillInfo]
    _toolsets: dict[str, FunctionToolset]

    def __init__(self) -> None:
        self._skills = {}
        self._toolsets = {}

    def register(self, name: str, description: str) -> FunctionToolset:
        """Register a skill and return its FunctionToolset for adding tools."""
        self._skills[name] = SkillInfo(name=name, description=description)
        toolset: FunctionToolset = FunctionToolset(
            metadata={SKILL_METADATA_KEY: name},
        )
        self._toolsets[name] = toolset
        return toolset

    def build_skillsets(self) -> list[AbstractToolset[Any]]:
        """Build the list of skills' toolsets to pass to Agent(toolsets=[...]).

        Returns a discovery toolset (always visible) and a filtered toolset
        that only exposes tools whose skill is currently active.
        """
        discovery = self._build_discovery_toolset()
        skill_toolsets = list(self._toolsets.values())
        if not skill_toolsets:
            return [discovery]
        combined = CombinedToolset(skill_toolsets)
        filtered = FilteredToolset(combined, skill_filter)
        return [discovery, filtered]

    def _build_discovery_toolset(self) -> FunctionToolset[SkillDeps]:
        manager = self

        discovery: FunctionToolset[SkillDeps] = FunctionToolset()

        @discovery.tool
        async def list_available_skills(ctx: RunContext[SkillDeps]) -> str:
            """List all skills that can be loaded. Shows name, status, and description."""
            lines: list[str] = []
            for info in manager._skills.values():
                status = "loaded" if info.name in ctx.deps.active_skills else "available"
                lines.append(f"- {info.name} [{status}]: {info.description}")
            return "\n".join(lines) or "No skills registered."

        @discovery.tool
        async def load_skill(ctx: RunContext[SkillDeps], skill_name: str) -> str:
            """Load a skill to make its tools available for use."""
            if skill_name not in manager._skills:
                available = ", ".join(manager._skills)
                return f"Unknown skill: {skill_name}. Available: {available}"
            if skill_name in ctx.deps.active_skills:
                return f"Skill '{skill_name}' is already loaded."
            ctx.deps.active_skills.add(skill_name)
            return f"Skill '{skill_name}' loaded. Its tools are now available."

        @discovery.tool
        async def unload_skill(ctx: RunContext[SkillDeps], skill_name: str) -> str:
            """Unload a skill to remove its tools from the active set."""
            if skill_name not in ctx.deps.active_skills:
                return f"Skill '{skill_name}' is not loaded."
            ctx.deps.active_skills.discard(skill_name)
            return f"Skill '{skill_name}' unloaded."

        return discovery
