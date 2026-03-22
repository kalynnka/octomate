from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from typing import Any, runtime_checkable

from pydantic_ai import RunContext, ToolDefinition
from pydantic_ai.toolsets import (
    AbstractToolset,
    CombinedToolset,
    FilteredToolset,
    FunctionToolset,
    PreparedToolset,
)
from typing_extensions import Protocol

from octomate.schemas.session import SessionKey

SKILL_METADATA_KEY = "skill"

ToolsPrepareFunc = Callable[
    [RunContext[Any], list[ToolDefinition]],
    Awaitable[list[ToolDefinition] | None],
]


@runtime_checkable
class SkillDeps(Protocol):
    active_skills: set[str]
    session_key: SessionKey


def skill_filter(ctx: RunContext[Any], tool_def: ToolDefinition) -> bool:
    skill_name = (tool_def.metadata or {}).get(SKILL_METADATA_KEY)
    if skill_name is None:
        return True
    deps = ctx.deps
    if not isinstance(deps, SkillDeps):
        return True
    return skill_name in deps.active_skills


def make_metadata_injector(
    skill_name: str,
    approvers: dict[str, list[str]] | None = None,
) -> ToolsPrepareFunc:
    """Create a prepare function that injects skill metadata into MCP tool definitions.

    When *approvers* is provided, tool definitions for tentacles with a matching
    entry are upgraded to ``kind='unapproved'`` so pydantic-ai triggers the
    human-in-the-loop approval flow.
    """

    async def inject(
        ctx: RunContext[Any], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        tentacle_id: str | None = None
        allowed: list[str] | None = None
        if approvers and isinstance(ctx.deps, SkillDeps):
            tentacle_id = ctx.deps.session_key.tentacle_id
            allowed = approvers.get(tentacle_id)

        result: list[ToolDefinition] = []
        for td in tool_defs:
            meta = dict(td.metadata) if td.metadata else {}
            meta[SKILL_METADATA_KEY] = skill_name
            if td.description:
                meta["description"] = td.description
            if allowed is not None:
                meta["approvers"] = allowed
                result.append(replace(td, metadata=meta, kind="unapproved"))
            else:
                result.append(replace(td, metadata=meta))
        return result

    return inject


@dataclass
class SkillInfo:
    name: str
    description: str


class SkillManager:
    skills: dict[str, SkillInfo]
    toolsets: dict[str, FunctionToolset]
    mcp_toolsets: dict[str, AbstractToolset[Any]]

    def __init__(self) -> None:
        self.skills = {}
        self.toolsets = {}
        self.mcp_toolsets = {}

    def register(self, name: str, description: str) -> FunctionToolset:
        """Register a skill and return its FunctionToolset for adding tools."""
        self.skills[name] = SkillInfo(name=name, description=description)
        toolset: FunctionToolset = FunctionToolset(
            metadata={SKILL_METADATA_KEY: name},
        )
        self.toolsets[name] = toolset
        return toolset

    def register_mcp(
        self,
        name: str,
        description: str,
        toolset: AbstractToolset[Any],
        approvers: dict[str, list[str]] | None = None,
    ) -> None:
        """Register an MCP-based skill.

        The toolset is wrapped to inject skill metadata so it participates
        in the load/unload gating via skill_filter.
        """
        self.skills[name] = SkillInfo(name=name, description=description)
        prepared = PreparedToolset(
            wrapped=toolset,
            prepare_func=make_metadata_injector(name, approvers),
        )
        self.mcp_toolsets[name] = prepared

    async def __aenter__(self):
        """Pre-enter MCP servers to pin the anyio cancel scope to this task.

        This prevents a RuntimeError when concurrent agent.run() calls race
        to close the MCP connection from different asyncio tasks.
        """
        self._exit_stack = AsyncExitStack()
        for server in self.mcp_toolsets.values():
            await self._exit_stack.enter_async_context(server)
        return self

    async def __aexit__(self, *args):
        await self._exit_stack.aclose()

    def build_skillsets(self) -> list[AbstractToolset[Any]]:
        """Build the list of skills' toolsets to pass to Agent(toolsets=[...]).

        Returns a discovery toolset (always visible) and a filtered toolset
        that only exposes tools whose skill is currently active.
        """
        discovery = self._build_discovery_toolset()
        all_toolsets: list[AbstractToolset[Any]] = list(self.toolsets.values())
        all_toolsets.extend(self.mcp_toolsets.values())
        if not all_toolsets:
            return [discovery]
        combined = CombinedToolset(all_toolsets)
        filtered = FilteredToolset(combined, skill_filter)
        return [discovery, filtered]

    def _build_discovery_toolset(self) -> FunctionToolset[SkillDeps]:
        manager = self

        discovery: FunctionToolset[SkillDeps] = FunctionToolset()

        @discovery.tool
        async def list_available_skills(ctx: RunContext[SkillDeps]) -> str:
            """List all skills that can be loaded. Shows name, status, and description."""
            lines: list[str] = []
            for info in manager.skills.values():
                if info.name in ctx.deps.active_skills:
                    status = "loaded"
                else:
                    status = "available"
                lines.append(f"- {info.name} [{status}]: {info.description}")
            return "\n".join(lines) or "No skills registered."

        @discovery.tool
        async def load_skill(ctx: RunContext[SkillDeps], skill_name: str) -> str:
            """Load a skill to make its tools available for use."""
            if skill_name not in manager.skills:
                available = ", ".join(manager.skills)
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
