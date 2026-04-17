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
    mcp_servers: dict[str, AbstractToolset[Any]]
    library: SkillLibrary | None
    _watch_task: asyncio.Task[None] | None
    _server_exit_stack: contextlib.AsyncExitStack | None

    def __init__(self) -> None:
        self.skills = {}
        self.toolsets = {}
        self.mcp_servers = {}
        self.library = None
        self._watch_task = None
        self._server_exit_stack = None

    def register(self, name: str, description: str) -> FunctionToolset:
        """Register a tool and return its FunctionToolset for adding tools."""
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
        """Register an MCP-based skill. All tools are prefixed with the skill name."""
        self.skills[name] = SkillInfo(name=name, description=description)
        prepared = PreparedToolset(
            wrapped=toolset,
            prepare_func=make_metadata_injector(name, approvers, tool_permissions),
        )
        self.mcp_servers[name] = PrefixedToolset(prepared, prefix=name)

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

    async def __aenter__(self):
        self._server_exit_stack = contextlib.AsyncExitStack()
        for server in self.mcp_servers.values():
            await self._server_exit_stack.enter_async_context(server)
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
        if self._server_exit_stack:
            await self._server_exit_stack.aclose()
            self._server_exit_stack = None

    def build_toolsets(self) -> list[AbstractToolset[Any]]:
        """Build the list of skills' toolsets to pass to Agent(toolsets=[...]).

        All skill tools are marked with defer_loading so they are hidden from
        the model's initial context.
        """
        all_toolsets: list[AbstractToolset[Any]] = list(self.toolsets.values())
        all_toolsets.extend(self.mcp_servers.values())
        if not all_toolsets:
            return []
        combined = CombinedToolset(all_toolsets)
        return [combined.defer_loading()]
