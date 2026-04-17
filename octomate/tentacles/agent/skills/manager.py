from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, replace
from typing import Any, Literal

from pydantic_ai import RunContext, ToolDefinition
from pydantic_ai.toolsets import (
    AbstractToolset,
    CombinedToolset,
    FunctionToolset,
    PrefixedToolset,
    PreparedToolset,
)

from octomate.tentacles.agent.context import SessionContext
from octomate.tentacles.agent.skills.library import SkillLibrary

logger = logging.getLogger(__name__)

SKILL_METADATA_KEY = "skill"

ToolPermission = Literal[
    "bypass",  # visible, never requires approval
    "default",  # visible, requires approval when approvers match the tentacle
    "masked",  # hidden from the agent entirely
]


def make_metadata_injector(
    skill_name: str,
    approvers: dict[str, list[str]] | None = None,
    tool_permissions: dict[str, ToolPermission] | None = None,
) -> Any:
    """Create a prepare function that injects skill metadata into MCP tool definitions."""

    async def inject(
        ctx: RunContext[Any], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        bypassed: list[ToolDefinition] = []
        defaulted: list[ToolDefinition] = []
        masked: list[str] = []
        for td in tool_defs:
            perm = (
                tool_permissions.get(td.name, "masked")
                if tool_permissions is not None
                else "default"
            )
            if perm == "masked":
                masked.append(td.name)
            elif perm == "bypass":
                bypassed.append(td)
            else:
                defaulted.append(td)

        if masked:
            logger.debug(
                "[%s] masked %d MCP tools: %s",
                skill_name,
                len(masked),
                ", ".join(sorted(masked)),
            )

        tentacle_approvers: list[str] | None = None
        if approvers and isinstance(ctx.deps, SessionContext):
            tentacle_approvers = approvers.get(ctx.deps.session_key.tentacle_id)

        result: list[ToolDefinition] = []
        for td in bypassed:
            meta = dict(td.metadata) if td.metadata else {}
            meta[SKILL_METADATA_KEY] = skill_name
            if td.description:
                meta["description"] = td.description
            result.append(replace(td, metadata=meta))
        for td in defaulted:
            meta = dict(td.metadata) if td.metadata else {}
            meta[SKILL_METADATA_KEY] = skill_name
            if td.description:
                meta["description"] = td.description
            if tentacle_approvers is not None:
                meta["approvers"] = tentacle_approvers
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
    mcp_servers: list[AbstractToolset[Any]]
    library: SkillLibrary | None

    def __init__(self) -> None:
        self.skills = {}
        self.toolsets = {}
        self.mcp_toolsets = {}
        self.mcp_servers = []
        self.library = None

    def register(self, name: str, description: str) -> FunctionToolset:
        """Register a skill and return its FunctionToolset for adding tools."""
        self.skills[name] = SkillInfo(name=name, description=description)
        toolset: FunctionToolset = FunctionToolset(
            metadata={SKILL_METADATA_KEY: name},
        )
        self.toolsets[name] = toolset
        return toolset

    def register_toolset(
        self,
        name: str,
        description: str,
        toolset: FunctionToolset,
    ) -> None:
        """Register a pre-built FunctionToolset under a named skill."""
        self.skills[name] = SkillInfo(name=name, description=description)
        self.toolsets[name] = toolset

    def register_mcp(
        self,
        name: str,
        description: str,
        toolset: AbstractToolset[Any],
        approvers: dict[str, list[str]] | None = None,
        tool_permissions: dict[str, ToolPermission] | None = None,
    ) -> None:
        """Register an MCP-based skill."""
        self._track_server(toolset)
        self.skills[name] = SkillInfo(name=name, description=description)
        prepared = PreparedToolset(
            wrapped=toolset,
            prepare_func=make_metadata_injector(name, approvers, tool_permissions),
        )
        self.mcp_toolsets[name] = prepared

    def register_skill_library(
        self,
        name: str,
        library: SkillLibrary,
        description: str = "Reusable prose guidance bundles. Call list_skills() to browse, load_skill(name) to read full instructions.",
    ) -> None:
        """Register a SkillLibrary as a discoverable skill toolset."""
        self.library = library
        self.skills[name] = SkillInfo(name=name, description=description)
        self.toolsets[name] = library.toolset

    def register_mcp_group(
        self,
        prefix: str,
        toolset: AbstractToolset[Any],
        categories: dict[str, tuple[str, dict[str, ToolPermission]]],
        approvers: dict[str, list[str]] | None = None,
    ) -> None:
        """Register multiple sub-skills from one MCP server."""
        self._track_server(toolset)
        for category, (description, perms) in categories.items():
            skill_name = f"{prefix}.{category}"
            self.skills[skill_name] = SkillInfo(
                name=skill_name, description=description
            )
            prepared = PreparedToolset(
                wrapped=toolset,
                prepare_func=make_metadata_injector(skill_name, approvers, perms),
            )
            self.mcp_toolsets[skill_name] = PrefixedToolset(prepared, prefix=prefix)

    def _track_server(self, toolset: AbstractToolset[Any]) -> None:
        if toolset not in self.mcp_servers:
            self.mcp_servers.append(toolset)

    async def __aenter__(self):
        self._exit_stack = contextlib.AsyncExitStack()
        for server in self.mcp_servers:
            await self._exit_stack.enter_async_context(server)
        self._watch_task: asyncio.Task | None = None
        if self.library:
            self._watch_task = asyncio.create_task(
                self.library.watch(), name="skill-watcher"
            )
        return self

    async def __aexit__(self, *args):
        if self._watch_task:
            self._watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch_task
        await self._exit_stack.aclose()

    def build_toolsets(self) -> list[AbstractToolset[Any]]:
        """Build the list of skills' toolsets to pass to Agent(toolsets=[...]).

        All skill tools are marked with defer_loading so they are hidden from
        the model's initial context.
        """
        all_toolsets: list[AbstractToolset[Any]] = list(self.toolsets.values())
        all_toolsets.extend(self.mcp_toolsets.values())
        if not all_toolsets:
            return []
        combined = CombinedToolset(all_toolsets)
        return [combined.defer_loading()]
