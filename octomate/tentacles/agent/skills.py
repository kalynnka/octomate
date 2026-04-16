from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic_ai import RunContext, ToolDefinition
from pydantic_ai.toolsets import (
    AbstractToolset,
    CombinedToolset,
    FunctionToolset,
    PreparedToolset,
)

from octomate.tentacles.agent.context import SessionContext

logger = logging.getLogger(__name__)

SKILL_METADATA_KEY = "skill"

ToolsPrepareFunc = Callable[
    [RunContext[Any], list[ToolDefinition]],
    Awaitable[list[ToolDefinition] | None],
]


ToolPermission = Literal[
    "bypass",  # visible, never requires approval
    "default",  # visible, requires approval when approvers match the tentacle
    "masked",  # hidden from the agent entirely
]

FRONTMATTER_DELIMITER = "---"


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


class SkillLibrary:
    docs: dict[str, SkillDoc]
    roots: list[Path]

    def __init__(self, roots: Sequence[Path | str]) -> None:
        self.roots = [Path(r) for r in roots]
        self.docs = {}

    def discover(self) -> None:
        """Scan all roots for */SKILL.md files and register valid ones.

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
                self.docs[name] = SkillDoc(
                    name=name,
                    description=description,
                    path=skill_md,
                    frontmatter=fm,
                    body=body,
                )
                logger.debug("skills: registered skill %r from %s", name, skill_md)

        logger.info("skills: discovered %d skill(s)", len(self.docs))

    def load(self, name: str) -> str:
        doc = self.docs.get(name)
        if doc is None:
            raise KeyError(f"skill {name!r} not found")
        return doc.body

    @cached_property
    def toolset(self) -> FunctionToolset[Any]:
        toolset: FunctionToolset[Any] = FunctionToolset()
        docs = self.docs

        @toolset.tool(requires_approval=False)
        async def list_skills(ctx: RunContext[Any]) -> list[dict[str, str]]:
            """List all available guidance skills by name and description.

            Use this to browse available skills. Each skill provides reusable
            step-by-step guidance for a specific task. Call load_skill(name)
            to read the full instructions for a skill that matches the user's request.
            """
            return [
                {"name": d.name, "description": d.description} for d in docs.values()
            ]

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

        return toolset


def make_metadata_injector(
    skill_name: str,
    approvers: dict[str, list[str]] | None = None,
    tool_permissions: dict[str, ToolPermission] | None = None,
) -> ToolsPrepareFunc:
    """Create a prepare function that injects skill metadata into MCP tool definitions.

    *tool_permissions* maps tool names to access levels:

    - ``bypass``  – visible, never requires approval
    - ``default`` – visible, requires approval when *approvers* match the tentacle
    - ``masked``  – hidden from the agent entirely

    Tools not listed in *tool_permissions* are treated as ``masked``.
    When *tool_permissions* is ``None``, all tools pass through with ``default``
    behaviour.
    """

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

    def __init__(self) -> None:
        self.skills = {}
        self.toolsets = {}
        self.mcp_toolsets = {}
        self.mcp_servers = []

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
        tool_permissions: dict[str, ToolPermission] | None = None,
    ) -> None:
        """Register an MCP-based skill.

        The toolset is wrapped to inject skill metadata.
        *tool_permissions* controls per-tool visibility and approval behaviour.
        """
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
        """Register a SkillLibrary as a discoverable skill toolset.

        The library's list_skills / load_skill tools flow through
        build_skillsets() exactly like any other FunctionToolset.
        """
        self.skills[name] = SkillInfo(name=name, description=description)
        self.toolsets[name] = library.toolset

    def register_mcp_group(
        self,
        prefix: str,
        toolset: AbstractToolset[Any],
        categories: dict[str, tuple[str, dict[str, ToolPermission]]],
        approvers: dict[str, list[str]] | None = None,
    ) -> None:
        """Register multiple sub-skills from one MCP server.

        Each entry in *categories* maps a category name to
        ``(description, tool_permissions)``.  Sub-skills are named
        ``{prefix}.{category}`` and can be loaded independently.
        """
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
            self.mcp_toolsets[skill_name] = prepared

    def _track_server(self, toolset: AbstractToolset[Any]) -> None:
        if toolset not in self.mcp_servers:
            self.mcp_servers.append(toolset)

    async def __aenter__(self):
        """Pre-enter MCP servers to pin the anyio cancel scope to this task.

        Each unique MCP server is entered exactly once (ref-counted internally
        by pydantic-ai).  The PreparedToolset wrappers in mcp_toolsets will
        re-enter the same server during agent.run(), safely incrementing the
        ref count.
        """
        self._exit_stack = AsyncExitStack()
        for server in self.mcp_servers:
            await self._exit_stack.enter_async_context(server)
        return self

    async def __aexit__(self, *args):
        await self._exit_stack.aclose()

    def build_toolsets(self) -> list[AbstractToolset[Any]]:
        """Build the list of skills' toolsets to pass to Agent(toolsets=[...]).

        All skill tools are marked with defer_loading so they are hidden from
        the model's initial context.  Pydantic-ai auto-injects a
        ``search_tools`` tool that lets the model discover them by keyword.
        """
        all_toolsets: list[AbstractToolset[Any]] = list(self.toolsets.values())
        all_toolsets.extend(self.mcp_toolsets.values())
        if not all_toolsets:
            return []
        combined = CombinedToolset(all_toolsets)
        return [combined.defer_loading()]
