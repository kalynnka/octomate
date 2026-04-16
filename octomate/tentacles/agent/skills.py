from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
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


_SCRIPT_TIMEOUT = 60  # seconds


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

    @cached_property
    def toolset(self) -> FunctionToolset[Any]:
        toolset: FunctionToolset[Any] = FunctionToolset()
        docs = self.docs

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
        """Pre-enter MCP servers and start the skill-file watcher (if a library is registered).

        Each unique MCP server is entered exactly once (ref-counted internally
        by pydantic-ai).  The PreparedToolset wrappers in mcp_toolsets will
        re-enter the same server during agent.run(), safely incrementing the
        ref count.
        """
        self._exit_stack = AsyncExitStack()
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
        the model's initial context.  Pydantic-ai auto-injects a
        ``search_tools`` tool that lets the model discover them by keyword.
        """
        all_toolsets: list[AbstractToolset[Any]] = list(self.toolsets.values())
        all_toolsets.extend(self.mcp_toolsets.values())
        if not all_toolsets:
            return []
        combined = CombinedToolset(all_toolsets)
        return [combined.defer_loading()]
