"""Project capability: how a thread says which project it is about.

A thread in no project runs in a workspace forked from nothing, thrown away when
the turn ends (OCTO-50). This is the door out of it: someone says what they want
worked on, the thread is bound to that project once, and its workspace is forked
ready for the turn after — the project's code, and a tree that is kept.

Two inputs are the agent's to judge and everything else is Octomate's — the path,
the mechanism, whether this user may bind at all, the branch the work lands on,
and the lifecycle. The two are the project, and the ref to start from: the default
branch is the wrong answer often enough — continuing someone's feature branch,
reproducing against a tag, working from a PR head — that a caller has to be able
to say otherwise.

Mounted user-scoped, so the registered-user gate is `for_profile` returning None
and no tool appears at all for a visitor, rather than a refusal they can argue
with.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict

from pydantic_ai import RunContext
from pydantic_ai.agent.abstract import AgentInstructions
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from octomate.capabilities.history import thread_id
from octomate.managers import ConversationManager, ThreadManager
from octomate.managers.mirrors import run_git
from octomate.managers.workspaces import WorkspaceManager
from octomate.schemas.user import UserProfile

PROJECT_INSTRUCTIONS = """\
## Working on a project

This conversation is in no project, so it runs in a workspace of its own that is
thrown away when the turn ends. You may write there, and nothing you write is
still there next turn.

- `list_projects()` — what this deployment knows, and what each project is.
- `work_on_project(name[, ref])` — say what this thread is about. `ref` is the
  branch, tag or commit the work starts from; omit it for the project's default.

Binding applies from your **next** turn. This run's working directory was fixed
when the process started, so nothing you do now can reach the workspace: say what
you will do once it applies, and let the person answer.

A thread binds once. If this one is already about a project, a different project
is a different thread — ask the person to start one.
"""


class ProjectSummary(TypedDict):
    """One registered project, as a model choosing between them needs it."""

    # Deliberately not the root: which absolute path a project is on the server is
    # the operator's business, and naming it in a chat thread is how it leaks.
    name: str
    description: str | None


def build_project_toolset(
    workspaces: WorkspaceManager,
    threads: ThreadManager,
    conversations: ConversationManager,
) -> FunctionToolset[None]:
    toolset: FunctionToolset[None] = FunctionToolset(id="projects")

    # `tool_plain` rather than `tool`: the listing is the same for every run, so
    # it needs no context and should not be handed one.
    @toolset.tool_plain
    async def list_projects() -> list[ProjectSummary]:
        """The projects this deployment can work on, and what each one is."""
        return [
            ProjectSummary(name=project.name, description=project.description)
            for project in workspaces.projects.list()
            if project.enabled
        ]

    @toolset.tool
    async def work_on_project(
        ctx: RunContext[None], name: str, ref: str | None = None
    ) -> str:
        """Say that this thread is about the project `name`, and fork its
        workspace. `ref` names the branch, tag or commit to start from; omit it
        for the project's default branch. Applies from the next turn.
        """
        project = workspaces.projects.get(name)
        if project is None or not project.enabled:
            available = sorted(
                other.name for other in workspaces.projects.list() if other.enabled
            )
            raise ModelRetry(
                f"No project called {name!r} is registered here. "
                f"Available: {', '.join(available) or 'none'}."
            )
        mirror = await workspaces.mirrors.sync(project)
        # Ahead of the bind, not after it, because a thread binds once: a ref that
        # turns out not to resolve would otherwise leave the thread committed to
        # the project with no way to ask again for the branch that was meant.
        if ref is not None and not await run_git("ls-remote", str(mirror), ref):
            raise ModelRetry(
                f"{project.name!r} has no {ref!r} to start from. Name a branch, "
                f"tag or commit its mirror has, or omit it for the default branch."
            )
        thread = await thread_id(ctx, conversations)
        await threads.bind(thread, project)
        await workspaces.materialize(workspaces.open(thread, project), mirror, ref)
        start = f" starting from {ref!r}" if ref is not None else ""
        return (
            f"This thread is about {project.name!r} now, and its workspace is "
            f"ready{start}. It applies from your next turn — this run is still in "
            f"the empty one it started in, and nothing you write there is kept. "
            f"Tell the person what you will do, and wait for them."
        )

    return toolset


@dataclass
class ProjectCapability(AbstractCapability[None]):
    """The tools a chat thread uses to become a working one."""

    workspaces: WorkspaceManager
    threads: ThreadManager
    conversations: ConversationManager
    toolset: AbstractToolset[None] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.toolset = build_project_toolset(
            self.workspaces, self.threads, self.conversations
        )

    async def for_profile(self, profile: UserProfile) -> ProjectCapability | None:
        """This capability for a registered user, and nothing for a visitor.

        Binding is a trust act: a project's own `AGENTS.md` reaches an agent as
        instructions, and any registered project may be bound by any registered
        user. The `users:` registry is the authority on who is a real person, and
        an ownerless profile is not one.

        The same instance serves everyone who passes, because nothing here is
        per-user — the gate is the whole of what the profile decides.
        """
        owner = await self.threads.users.owner(profile)
        return self if owner is not None else None

    def get_toolset(self) -> AbstractToolset[None] | None:
        return self.toolset

    def get_instructions(self) -> AgentInstructions[None] | None:
        return PROJECT_INSTRUCTIONS
