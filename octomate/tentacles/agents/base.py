from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from functools import cached_property
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar, TypeAlias, TypeVar, overload

from pydantic_ai import (
    AgentCapability,
    AgentModelSettings,
    AgentNativeTool,
    AgentRunResult,
    AgentSpec,
    RunUsage,
    UsageLimits,
)
from pydantic_ai.agent.abstract import (
    AgentInstructions,
    AgentMetadata,
    EventStreamHandler,
    RunOutputDataT,
)
from pydantic_ai.messages import UserContent
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.output import OutputSpec
from pydantic_ai.settings import ThinkingEffort
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai.toolsets import AbstractToolset

from octomate.capabilities.harness.react import ReactEventStream
from octomate.config.agents import AgentRouteModelName
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.project import Project
from octomate.schemas.triage import AgentRoute, Claim
from octomate.schemas.user import UserProfile
from octomate.tentacles.base import Tentacle
from octomate.types.json import JsonObject

if TYPE_CHECKING:
    from octomate.capabilities.harness.deferred import DeferredSuspender

# The tentacle's output type is whatever its builder's agent produces (a deferring
# agent includes DeferredToolRequests in it); run-level output_type overrides are
# generic over RunOutputDataT, mirroring pydantic-ai's own run signatures.
AgentOutputT = TypeVar("AgentOutputT")
AgentDepsT = TypeVar("AgentDepsT")
AgentSpecInput: TypeAlias = JsonObject | AgentSpec


class AgentTentacle(Tentacle[AgentOutputT, AgentDepsT], ABC):
    """Base class for Octomate agents wrapping pydantic-ai run entrypoints."""

    # Capability blurb the triage agent reads when routing to a reception agent.
    # Subclasses refine this default; overridable at init.
    description: str = "General-purpose agent for handling user requests."

    # Per-model claims this agent advertises, keyed by route model name. Claims
    # are the agent's to make — its config block owns them; a channel only
    # chooses which agents to expose (and their entry models). A model with no
    # claim advertises nothing: it is not offered as a route, so it cannot be
    # summoned (or commissioned). Subclasses assign it in `__init__`; the default
    # is read-only, so the empty one cannot be shared into.
    claims: Mapping[AgentRouteModelName, Claim] = MappingProxyType({})

    # Whether this agent's driven turns offer the gateway spells — the agent's side
    # of the switch; the channel-agent connection's `gateway` is the other, and both
    # must be on. Subclasses assign it from their config in `__init__`.
    gateway: bool = True

    @cached_property
    def routes(self) -> list[AgentRoute]:
        """The routes this agent offers — one per claim it can actually honor.
        A claim naming a model that is not in `models` is evicted rather than
        advertised, so a route can never point at a model the agent cannot run.
        Cached — `claims` and `models` are settled in `__init__` and never
        change after."""
        return [
            AgentRoute(agent_id=self.id, model=model, claim=claim)
            for model, claim in self.claims.items()
            if model in self.models
        ]

    # Whether the agent keeps a live in-process run that can park on a human
    # deferral (approval/question) and resume by delivering the response to its
    # waiter, instead of resuming durably through the triage graph. In-process
    # agents populate `pending` (batch id -> waiter); the rest never touch it.
    in_process: ClassVar[bool] = False
    pending: dict[uuid.UUID, asyncio.Future[DeferredActionBatchResponse]]

    # The approval postures this agent answers to, in its own provider's vocabulary.
    # A fact about the class rather than the row: what Claude accepts does not depend
    # on which conversation is asking, or on the id this tentacle was registered under.
    permission_modes: ClassVar[tuple[str, ...]] = ()

    @property
    def default_permission_mode(self) -> str | None:
        """The posture this agent's conversations run under when they declare none —
        its configured default, in its own vocabulary.

        A row's NULL is not "no posture", it is "nothing said here"; this is what
        decides instead, and it is what the console reports for such a row rather than
        showing a blank. None only for an agent that answers to no posture at all."""
        return None

    models: dict[AgentRouteModelName, Model | str]

    async def user_capabilities(
        self,
        profile: UserProfile,
    ) -> list[AgentCapability[AgentDepsT]]:
        """Build capabilities whose credentials belong to this run's user."""
        return []

    async def run_project(self, thread_id: uuid.UUID) -> Project | None:
        """The registered project a run in this thread is in, or None when it is in
        none.

        The thread is where a project is bound, and every conversation belongs to
        one, so this is the whole of "which project is this work in" — asked of the
        thread each run rather than copied onto the conversation.

        A thread naming a project the registry no longer carries is in none, and so is
        one whose project is disabled: the row is retained either way, but a project
        that is not registered — or whose root disk has lost — has no root to offer.
        """
        if not self.octomate.projects.roots:
            # Nothing registered: no thread can be in a project, so a run is what it
            # was before there were projects, down to not reading the thread.
            return None
        thread = await self.octomate.thread_manager.get(thread_id)
        if thread is None:
            raise ValueError(f"unknown thread {thread_id}")
        attributed = await thread.project
        if attributed is None:
            return None
        # Through the registry rather than off the row, which is retained when the
        # project stops being registered.
        project = self.octomate.projects.get(attributed.name)
        return project if project is not None and project.enabled else None

    async def run_cwd(self, thread_id: uuid.UUID, agent_cwd: str) -> str:
        """The directory a run in this thread happens in: its project's root, or
        `agent_cwd` when the thread is in no project.

        `agent_cwd` is the agent's own configured directory, and is where a thread in
        no project runs — which is every thread until a project claims one, and what
        keeps dispatch exactly where it has always been.

        Always absolute, because the run records this: an agent's `cwd` defaults to
        `"."`, which hangs off wherever Octomate was started and names nothing once the
        process is gone. Symlinks are left alone, exactly as a project root leaves
        them — the registry resolves both sides when it compares.
        """
        project = await self.run_project(thread_id)
        if project is None:
            return str(Path(agent_cwd).absolute())
        return str(project.root)

    @overload
    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: ThinkingEffort | None = None,
        conversation_id: uuid.UUID | None = None,
        interactive: bool = True,
        instructions: AgentInstructions[AgentDepsT] = None,
        deps: AgentDepsT = None,
        model_settings: AgentModelSettings[AgentDepsT] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[AgentDepsT] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[AgentDepsT]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[AgentDepsT]] | None = None,
        event_stream_handler: EventStreamHandler[AgentDepsT] | None = None,
        capabilities: Sequence[AgentCapability[AgentDepsT]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AgentRunResult[AgentOutputT]: ...

    @overload
    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT],
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: ThinkingEffort | None = None,
        conversation_id: uuid.UUID | None = None,
        interactive: bool = True,
        instructions: AgentInstructions[AgentDepsT] = None,
        deps: AgentDepsT = None,
        model_settings: AgentModelSettings[AgentDepsT] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[AgentDepsT] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[AgentDepsT]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[AgentDepsT]] | None = None,
        event_stream_handler: EventStreamHandler[AgentDepsT] | None = None,
        capabilities: Sequence[AgentCapability[AgentDepsT]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AgentRunResult[RunOutputDataT]: ...

    @abstractmethod
    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: ThinkingEffort | None = None,
        conversation_id: uuid.UUID | None = None,
        interactive: bool = True,
        instructions: AgentInstructions[AgentDepsT] = None,
        deps: AgentDepsT = None,
        model_settings: AgentModelSettings[AgentDepsT] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[AgentDepsT] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[AgentDepsT]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[AgentDepsT]] | None = None,
        event_stream_handler: EventStreamHandler[AgentDepsT] | None = None,
        capabilities: Sequence[AgentCapability[AgentDepsT]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AgentRunResult[AgentOutputT | RunOutputDataT]:
        """Run the agent for an Octomate conversation."""

    async def subagent_run(
        self: AgentTentacle[AgentOutputT, None],
        user_prompt: str,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID,
        conversation_id: uuid.UUID,
        run_name: str | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: ThinkingEffort | None = None,
        instructions: str | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
    ) -> AgentRunResult[AgentOutputT]:
        """Run as a subagent: the subagent contract in one place, apart from
        `run()`'s general surface.

        A subagent run is addressed at a pre-ensured conversation its spawner
        owns (`conversation_id` is required, not optional), and it is fully
        non-interactive — every human interaction declines at once. The caller
        controls `capabilities` outright: a subagent mounts exactly what its
        spawner passes, never the tentacle's own set (inkling overrides to keep
        its defaults out; claude/codex ignore capabilities entirely). The
        spawner passes its framing as `instructions` and stamps the run tree
        after the report returns.
        """
        return await self.run(
            user_prompt,
            conversation_address=conversation_address,
            thread_id=thread_id,
            conversation_id=conversation_id,
            interactive=False,
            run_name=run_name,
            model=model,
            effort=effort,
            instructions=instructions,
            capabilities=capabilities,
        )

    @overload
    def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: ThinkingEffort | None = None,
        conversation_id: uuid.UUID | None = None,
        interactive: bool = True,
        instructions: AgentInstructions[AgentDepsT] = None,
        deps: AgentDepsT = None,
        model_settings: AgentModelSettings[AgentDepsT] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[AgentDepsT] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[AgentDepsT]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[AgentDepsT]] | None = None,
        capabilities: Sequence[AgentCapability[AgentDepsT]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> ReactEventStream[AgentOutputT]: ...

    @overload
    def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT],
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: ThinkingEffort | None = None,
        conversation_id: uuid.UUID | None = None,
        interactive: bool = True,
        instructions: AgentInstructions[AgentDepsT] = None,
        deps: AgentDepsT = None,
        model_settings: AgentModelSettings[AgentDepsT] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[AgentDepsT] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[AgentDepsT]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[AgentDepsT]] | None = None,
        capabilities: Sequence[AgentCapability[AgentDepsT]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> ReactEventStream[RunOutputDataT]: ...

    @abstractmethod
    def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: ThinkingEffort | None = None,
        conversation_id: uuid.UUID | None = None,
        interactive: bool = True,
        instructions: AgentInstructions[AgentDepsT] = None,
        deps: AgentDepsT = None,
        model_settings: AgentModelSettings[AgentDepsT] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[AgentDepsT] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[AgentDepsT]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[AgentDepsT]] | None = None,
        capabilities: Sequence[AgentCapability[AgentDepsT]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> ReactEventStream[AgentOutputT | RunOutputDataT]:
        """Stream raw agent events for an Octomate conversation."""
