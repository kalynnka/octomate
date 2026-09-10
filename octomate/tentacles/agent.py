from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import aclosing, asynccontextmanager
from functools import cached_property
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import TYPE_CHECKING, ClassVar, Self, TypeVar, overload

import anyio
from pydantic_ai import (
    AgentCapability,
    AgentModelSettings,
    AgentNativeTool,
    AgentRunResult,
    AgentSpec,
    RunUsage,
    ToolDenied,
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

from octomate.capabilities.harness.react import ReactEventStream, ReactStreamEvent
from octomate.config.agents import AgentRouteModelName
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress, Conversation
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
type AgentSpecInput = JsonObject | AgentSpec


class AgentTentacle(Tentacle[AgentOutputT, AgentDepsT], ABC):
    """Base class for Octomate agents wrapping pydantic-ai run entrypoints."""

    # Capability blurb the triage agent reads when routing to a reception agent.
    # Subclasses refine this default; overridable at init.
    description: str = "General-purpose agent for handling user requests."

    # Routing metadata supplied by the harness, or config when it is unavailable.
    claims: Mapping[AgentRouteModelName, Claim] = MappingProxyType({})

    # Whether this agent's driven turns offer the gateway spells — the agent's side
    # of the switch; the channel-agent connection's `gateway` is the other, and both
    # must be on. Subclasses assign it from their config in `__init__`.
    gateway: bool = True

    # Native hook/stream identity for this runtime, shared by its configured agents.
    native_id: ClassVar[str | None] = None

    @cached_property
    def driven_sessions(self) -> Counter[str]:
        """Live runs holding each external runtime session on this agent."""
        return Counter()

    @cached_property
    def native_sessions(self) -> Counter[str]:
        """Accepted native transcript streams currently attached to this agent."""
        return Counter()

    @cached_property
    def run_tasks(self) -> set[asyncio.Task[None]]:
        """Collectors registered by agent harnesses that opt into `observe_run`.

        Created on first access. Participating harnesses drain these tasks
        during shutdown; other harnesses do not need this registry.
        """
        return set()

    async def observe_run(
        self, events: AsyncGenerator[ReactStreamEvent[RunOutputDataT], None]
    ) -> AsyncGenerator[ReactStreamEvent[RunOutputDataT], None]:
        """Collect a run to completion even when its observer closes or cancels.

        Agent harnesses opt in by wrapping their event generators with this
        method and draining `run_tasks` during shutdown. Inheriting it alone
        does not change a harness's lifecycle.

        Joining before leaving keeps the caller's gateway and other enclosing
        resources available to the agent until its final notifications are recorded.
        """
        send, receive = anyio.create_memory_object_stream[
            ReactStreamEvent[RunOutputDataT]
        ](100)
        errors: list[Exception | asyncio.CancelledError] = []

        async def collect() -> None:
            try:
                async with aclosing(events):
                    async for event in events:
                        try:
                            await send.send(event)
                        except anyio.BrokenResourceError:
                            # Only the observer is gone; keep ingesting the run.
                            pass
            except (Exception, asyncio.CancelledError) as error:
                errors.append(error)
            finally:
                send.close()

        task = asyncio.create_task(collect())
        self.run_tasks.add(task)
        observed_end = False
        cancelled = False
        try:
            async with receive:
                async for event in receive:
                    yield event
            observed_end = True
            for error in errors:
                raise error
        finally:
            receive.close()
            # AnyIO scopes repeatedly cancel at checkpoints; asyncio callers can
            # also cancel more than once. Neither may cancel the collector.
            with anyio.CancelScope(shield=True):
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        cancelled = True
            self.run_tasks.discard(task)
            if not observed_end:
                for error in errors:
                    logging.getLogger(type(self).__module__).error(
                        "Agent %s run failed after observer detached",
                        self.id,
                        exc_info=error,
                    )
            if cancelled:
                raise asyncio.CancelledError

    @cached_property
    def routes(self) -> list[AgentRoute]:
        """Every served model, using discovered or configured routing metadata."""
        return self.build_routes()

    @asynccontextmanager
    async def driving(
        self, session_id: str, *, native: bool = False
    ) -> AsyncGenerator[None]:
        """Count a driven runtime session or an accepted native stream.

        Driven runs prepare their workspace before claiming a known runtime
        session id. Claim before activity that Octomate's native endpoints ingest,
        and hold through the run's cleanup, including interruption or cancellation;
        release it before recording the run and before leaving the workspace.
        Session creation may precede the claim only if native ingest ignores it.

        Native streams claim after their handshake is accepted and keep the claim
        through stream cleanup. Probes without a workspace, such as model
        discovery, still claim before connecting to a hook-emitting runtime.
        """
        sessions = self.native_sessions if native else self.driven_sessions
        sessions[session_id] += 1
        try:
            yield
        finally:
            sessions[session_id] -= 1
            if sessions[session_id] == 0:
                del sessions[session_id]

    def should_ingest_session(self, session_id: str) -> bool:
        """Native endpoints are shared by all configured agents for a runtime."""
        if session_id in self.driven_sessions:
            return False
        return self.native_id is None or not any(
            agent.native_id == self.native_id and session_id in agent.driven_sessions
            for agent in self.octomate.agents.values()
        )

    def build_routes(self) -> list[AgentRoute]:
        return [
            AgentRoute(
                agent_id=self.id,
                model=model,
                claim=self.claims.get(model) or Claim(self.description, efforts=()),
            )
            for model in self.models
        ]

    async def __aenter__(self) -> Self:
        """Enter after the subclass has prepared its models and claims."""
        self.routes = self.build_routes()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        self.routes = []

    @property
    def default_model(self) -> str | None:
        """Inkling's configured first model; harnesses override with native defaults."""
        return next(iter(self.models), None)

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

    async def discover_models(self) -> None:
        """Refresh models and claims once the harness connection is ready.

        Harnesses call this during entry and install results with set_model_catalog.
        Agents with config-supplied catalogs keep their existing models and claims.
        """

    def set_model_catalog(
        self, models: dict[str, Model | str], claims: dict[str, Claim]
    ) -> None:
        self.models = models
        self.claims = claims
        self.routes = self.build_routes()

    async def user_capabilities(
        self,
        profile: UserProfile,
    ) -> list[AgentCapability[AgentDepsT]]:
        """Build capabilities whose credentials belong to this run's user."""
        return []

    def resumed_prompt(self, results: DeferredToolResults) -> str:
        """What a resumed run opens with, for a runtime that takes no tool result
        back: what the graph resolved the deferral with, spoken as its next prompt
        — the sentence of a deferral the graph performed itself, or a person's
        answers and verdicts on a batch they came back to."""
        spoken: list[str] = []
        for value in results.calls.values():
            spoken.append(
                "\n".join(str(one) for one in value)
                if isinstance(value, list)
                else str(value)
            )
        for verdict in results.approvals.values():
            if isinstance(verdict, ToolDenied):
                spoken.append(f"Denied: {verdict.message}")
            else:
                spoken.append("Approved." if verdict else "Denied.")
        return "\n\n".join(spoken)

    async def relocate(self, conversation: Conversation, *, cwd: Path) -> None:
        """Relocate the runtime session behind `conversation` to `cwd`, where its next
        run resumes. The graph's call, made once a teleport has settled where the
        agent lands; the tentacle only knows how to move one.

        Nothing to move for a runtime that resumes a session from anywhere. Claude
        files one under the directory it ran in, and every teleport changes that
        directory — a new thread's workspace, or the project's — so a driven Claude
        session needs this on each. A native session's transcript is on its own
        machine, so a native teleport has nothing here to move: bringing it over is
        a rebuild from the ledger, still to come, and this is where it lands."""

    async def run_project(self, thread_id: uuid.UUID) -> Project | None:
        """The project a run in this thread is in, or None when it is in none.

        The thread is where a project is bound, and every conversation belongs to
        one, so this is the whole of "which project is this run in" — asked of the
        thread each run rather than copied onto the conversation. Reading the
        thread is what this adds; judging what it names is the registry's, and is
        `ProjectManager.of`.
        """
        if not self.octomate.projects.roots:
            # Nothing registered: no thread can be in a project, so a run is what it
            # was before there were projects, down to not reading the thread.
            return None
        thread = await self.octomate.thread_manager.get(thread_id)
        if thread is None:
            raise ValueError(f"unknown thread {thread_id}")
        return await self.octomate.projects.of(thread)

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
