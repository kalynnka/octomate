from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self, TypeAlias, get_args, overload

from pydantic_ai import (
    AgentCapability,
    AgentModelSettings,
    AgentNativeTool,
    AgentRunResult,
    AgentRunResultEvent,
    RunUsage,
    UsageLimits,
)
from pydantic_ai.agent import EventStreamHandler
from pydantic_ai.agent.abstract import (
    AgentInstructions,
    AgentMetadata,
    RunOutputDataT,
)
from pydantic_ai.capabilities import Toolset
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import UserContent
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.output import OutputSpec
from pydantic_ai.settings import ModelSettings, ThinkingEffort, merge_model_settings
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.toolsets import AbstractToolset, ApprovalRequiredToolset
from pydantic_ai_harness.filesystem import FileSystem
from pydantic_ai_harness.repo_context import RepoContext
from pydantic_ai_harness.shell import LLM_API_KEY_ENV_PATTERNS, Shell

from octomate.capabilities import UserScopedCapability
from octomate.capabilities.harness.agent import Agent
from octomate.capabilities.harness.deferred import (
    DeclineResolver,
    DeferredResolver,
    DeferredSuspender,
    PostureResolver,
)
from octomate.capabilities.harness.react import (
    ReactDeps,
    ReactEventStream,
    ReactState,
    ReactStreamEvent,
    ResumeTurn,
    StartTurn,
    iter_react_graph_events,
)
from octomate.config.agents import AgentRouteModelName
from octomate.managers.conversation import ConversationManager
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.project import Project
from octomate.schemas.segments import MessageSegment
from octomate.schemas.triage import Claim
from octomate.schemas.user import UserProfile
from octomate.telemetry import inkling_logfire
from octomate.tentacles.agents.base import (
    AgentSpecInput,
    AgentTentacle,
)
from octomate.tentacles.agents.inkling.prompts import SYSTEM_PROMPT
from octomate.types.permissions import InklingPermissionMode

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)

# Startup MCP warming is best-effort; cap it so a stuck MCP server (one that
# connects but never answers `initialize`/`tools/list`) degrades to warming on
# demand instead of hanging the host's startup.
MCP_WARM_TIMEOUT = 20.0

InklingOutput: TypeAlias = str | list[MessageSegment] | DeferredToolRequests


@dataclass(frozen=True)
class InklingDeferrals:
    """Which deferrals Inkling answers itself, decided again at each one.

    The react loop asks this per deferral rather than once per run, so a posture
    switched while the run is going is what the next round runs under — the round
    after the switch, not the run after it. Only the two things fixed for the whole
    run are held here; the posture is read off the conversation each time.

    The chain reads in the order it answers. The posture speaks first and only for
    approvals and questions; a run with no human closes with a catch-all, because it
    has no suspender either and a batch left part-answered would end the run holding
    requests nobody is coming for. An empty chain sends the batch to a human, which
    is what `default` means.
    """

    # Whether anyone is there to ask. A commissioned accomplice has nobody, which is
    # what puts the catch-all on the end rather than what silences the posture.
    interactive: bool
    # The agent's configured default, which decides for a conversation that declares
    # nothing — a NULL column says "nothing was said here", not "no posture".
    configured: InklingPermissionMode
    # What `default` falls back on — the tentacle's injected resolver, normally None,
    # which is the deferral reaching a human through the suspender.
    fallback: DeferredResolver | None

    def __call__(self, conversation: Conversation) -> list[DeferredResolver]:
        chain: list[DeferredResolver] = []
        match conversation.permission_mode or self.configured:
            case "dontAsk":
                # Claude's own reading of the word, which this vocabulary is: deny
                # what would have needed asking, and leave the agent to decide.
                chain.append(
                    PostureResolver(
                        approve=False,
                        message="Not asked, and not approved: the user has said they "
                        "do not want questions or approval prompts in this "
                        "conversation. Nothing was put to them and nothing will be — "
                        "decide this yourself, and say in your report what you "
                        "assumed and why.",
                    )
                )
            case "bypassPermissions":
                chain.append(
                    PostureResolver(
                        approve=True,
                        message="Not asked: the user has said they do not want "
                        "questions in this conversation, and approvals go through "
                        "without one. Nothing was put to them and nothing will be — "
                        "decide this yourself, and say in your report what you "
                        "assumed and why.",
                    )
                )
            case _ if self.fallback is not None:
                chain.append(self.fallback)
        if not self.interactive:
            chain.append(DeclineResolver())
        return chain


@dataclass
class InklingTentacle(AgentTentacle[InklingOutput, None]):
    """Inkling agent wrapper with pydantic-ai-style run entrypoints."""

    agent: Agent[None, InklingOutput] = field(init=False)
    # Held on the tentacle, not baked into the Agent: every run decides what to
    # mount — an accomplice run gets none of them.
    capabilities: list[AgentCapability[None]] = field(init=False)
    # The mounted capabilities that serve a run through a per-user copy instead of
    # themselves; held apart from `capabilities` so a run never mounts the unbound
    # original beside the copy.
    user_scoped_capabilities: list[UserScopedCapability[None]] = field(init=False)
    conversation_manager: ConversationManager = field(init=False)
    deferred_resolver: DeferredResolver | None = None
    # The posture a conversation that declares none of its own runs under. Unlike
    # claude and codex, inkling takes no config object, so its default arrives here.
    permission_mode: InklingPermissionMode = "default"
    request_limit: int = 256

    permission_modes: ClassVar[tuple[str, ...]] = get_args(InklingPermissionMode)

    @property
    def default_permission_mode(self) -> str | None:
        return self.permission_mode

    description: str = (
        "General assistant for conversation, questions, writing, analysis, and "
        "coordinating multi-step work."
    )

    _exit_stack: AsyncExitStack = field(init=False)
    toolsets: list[AbstractToolset[None]] = field(init=False)
    # Background MCP/agent warming, spawned by `__aenter__`; `__aexit__` settles it.
    warm_task: asyncio.Task[None] | None = field(init=False)

    def __init__(
        self,
        id: str,
        octomate: Octomate,
        *,
        agent: Agent[None, InklingOutput] | None = None,
        models: Mapping[AgentRouteModelName, Model | str] | None = None,
        name: str = "octomate-inkling",
        toolsets: Sequence[AbstractToolset[None]] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        conversation_manager: ConversationManager | None = None,
        deferred_resolver: DeferredResolver | None = None,
        description: str | None = None,
        claims: Mapping[KnownModelName, Claim] | None = None,
        permission_mode: InklingPermissionMode = "default",
        request_limit: int = 256,
        gateway: bool = True,
    ) -> None:
        super().__init__(id=id, octomate=octomate)
        self.permission_mode = permission_mode
        self.request_limit = request_limit
        self.gateway = gateway
        self.models = dict(models or {})
        self.capabilities = [
            capability
            for capability in (capabilities or [])
            if not isinstance(capability, UserScopedCapability)
        ]
        self.user_scoped_capabilities = [
            capability
            for capability in (capabilities or [])
            if isinstance(capability, UserScopedCapability)
        ]
        if agent is None:
            default_model = next(iter(self.models.values()), None)
            if default_model is None:
                raise ValueError("InklingTentacle requires at least one model")
            agent = Agent(
                default_model,
                deps_type=type(None),
                name=name,
                output_type=[str, list[MessageSegment], DeferredToolRequests],
                toolsets=list(toolsets or []),
                system_prompt=system_prompt,
            )
        self.agent = agent
        # Default to the project-level manager so every agent conversation shares
        # one source of truth with the thread ledger and the rest of Octomate; an
        # explicit manager still wins.
        self.conversation_manager = conversation_manager or octomate.conversations
        self.deferred_resolver = deferred_resolver
        self.description = description or self.description
        # Not `dict(...)`, which C416 asks for: each config's claims are keyed by that
        # runtime's own narrower literal, and `Mapping`'s key is invariant — only the
        # comprehension widens them to `AgentRouteModelName` without a cast.
        self.claims = {  # noqa: C416
            model: claim for model, claim in (claims or {}).items()
        }
        self._exit_stack = AsyncExitStack()
        self.toolsets = list(toolsets or [])
        self.warm_task = None

    async def user_capabilities(
        self,
        profile: UserProfile,
    ) -> list[AgentCapability[None]]:
        # Concurrently: binding an integration can cold-build the user's MCP
        # session (connect + tools/list), so the cost is the slowest server
        # rather than the sum of them.
        bound = await asyncio.gather(
            *(
                capability.for_profile(profile)
                for capability in self.user_scoped_capabilities
            )
        )
        return [capability for capability in bound if capability is not None]

    def project_capabilities(self, project: Project) -> list[AgentCapability[None]]:
        """What a run gets for working in `project`: its instructions, and the tools
        to act in it. A run in no project gets none of these and is a conversation.

        Every one is rooted at `project.root`, and nothing widens that. `extra_roots`
        are left out — a settings tree is not where a repo keeps its `AGENTS.md`, and
        a second root is a second place to write. `RepoContext`'s walk-up stays off
        (`home_dir=None`), so an ancestor's instructions never load; the operator's own
        `~/.claude/CLAUDE.md` is what that protects.
        """
        return [
            RepoContext[None](workspace_dir=project.root),
            Toolset(
                ApprovalRequiredToolset(
                    wrapped=FileSystem[None](root_dir=project.root).get_toolset()
                )
            ),
            Toolset(
                ApprovalRequiredToolset(
                    wrapped=Shell[None](
                        cwd=project.root,
                        # A repo's own `AGENTS.md` reaches this agent as instructions,
                        # and now those instructions have a shell. The provider keys
                        # this process runs on are not the project's to spend.
                        denied_env_patterns=LLM_API_KEY_ENV_PATTERNS,
                    ).get_toolset()
                )
            ),
        ]

    async def __aenter__(self) -> Self:
        # A capability holding warm resources of its own (the per-user GitHub MCP
        # sessions) opens for the tentacle's lifetime, so the copies serving individual
        # runs find them warm.
        for capability in (*self.capabilities, *self.user_scoped_capabilities):
            if isinstance(capability, AbstractAsyncContextManager):
                await self._exit_stack.enter_async_context(capability)
        # Warming runs behind the tentacle, not in front of it: entering returns
        # immediately, so the host never waits on an MCP server and the agent takes
        # messages from its first moment — a run that lands before warming finishes
        # enters the cold toolsets inside its own run (reference-counted) and pays
        # the listing latency once.
        self.warm_task = asyncio.create_task(self.warm())
        return self

    async def warm(self) -> None:
        # Warm the operator MCP toolsets, all concurrently — cost is the slowest
        # server, not the sum. Opening the session and priming `tools/list` (the
        # first run would otherwise block on a multi-second listing) keeps a warm
        # reference for the tentacle's lifetime; the agent enter below reuses it
        # (reference-counted). Each server's own connect budget rides on its toolset
        # as `init_timeout`; this bound only keeps one stuck server warming forever.
        servers: list[MCPToolset[None]] = []

        def collect(candidate: AbstractToolset[None]) -> None:
            if isinstance(candidate, MCPToolset):
                servers.append(candidate)

        async def warm_toolset(
            toolset: AbstractToolset[None], servers: list[MCPToolset[None]]
        ) -> None:
            try:
                with inkling_logfire.span(
                    "inkling.warm_mcp_tools",
                    mcp_servers=[server.id for server in servers],
                ):
                    await asyncio.wait_for(
                        self._exit_stack.enter_async_context(toolset),
                        timeout=MCP_WARM_TIMEOUT,
                    )
                    await asyncio.wait_for(
                        asyncio.gather(
                            *(server.list_tools() for server in servers),
                            return_exceptions=True,
                        ),
                        timeout=MCP_WARM_TIMEOUT,
                    )
            except Exception:
                logger.warning(
                    "Agent %s: failed to warm MCP server(s) %s at startup; "
                    "runs will reconnect on demand",
                    self.id,
                    [server.id for server in servers],
                    exc_info=True,
                )

        warmable: list[tuple[AbstractToolset[None], list[MCPToolset[None]]]] = []
        for toolset in self.toolsets:
            servers.clear()
            toolset.apply(collect)
            if servers:
                warmable.append((toolset, list(servers)))
        await asyncio.gather(
            *(warm_toolset(toolset, servers) for toolset, servers in warmable)
        )
        # Enter the wrapped agent once for the tentacle's lifetime so any toolset it
        # holds that was not warmed above (e.g. one passed directly rather than via
        # `toolsets=`) opens too and the agent stays warm; already-open servers
        # refcount instantly.
        try:
            await asyncio.wait_for(
                self._exit_stack.enter_async_context(self.agent),
                timeout=MCP_WARM_TIMEOUT,
            )
        except Exception:
            logger.warning(
                "Agent %s: failed to warm agent session at startup "
                "(a stuck MCP server times out here); runs will reconnect on demand",
                self.id,
                exc_info=True,
            )

    async def __aexit__(self, *exc: object) -> None:
        # Settle warming before the stack unwinds: an exit racing a half-opened
        # toolset would close sessions the warm task is still entering.
        if self.warm_task is not None:
            self.warm_task.cancel()
            try:
                await self.warm_task
            except asyncio.CancelledError:
                pass
            self.warm_task = None
        await self._exit_stack.aclose()

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
        instructions: AgentInstructions[None] = None,
        deps: None = None,
        model_settings: AgentModelSettings[None] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[None] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[None]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[None]] | None = None,
        event_stream_handler: EventStreamHandler[None] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AgentRunResult[InklingOutput]: ...

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
        instructions: AgentInstructions[None] = None,
        deps: None = None,
        model_settings: AgentModelSettings[None] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[None] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[None]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[None]] | None = None,
        event_stream_handler: EventStreamHandler[None] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AgentRunResult[RunOutputDataT]: ...

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
        instructions: AgentInstructions[None] = None,
        deps: None = None,
        model_settings: AgentModelSettings[None] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[None] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[None]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[None]] | None = None,
        event_stream_handler: EventStreamHandler[None] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AgentRunResult[InklingOutput | RunOutputDataT]:
        result: AgentRunResult[InklingOutput | RunOutputDataT] | None = None
        async for event in self.iter_graph_events(
            user_prompt=user_prompt,
            conversation_address=conversation_address,
            thread_id=thread_id,
            source_thread_address=source_thread_address,
            source_thread_message_ids=source_thread_message_ids,
            run_name=run_name,
            output_type=output_type,
            deferred_tool_results=deferred_tool_results,
            deferred_suspender=deferred_suspender,
            model=model,
            effort=effort,
            conversation_id=conversation_id,
            interactive=interactive,
            instructions=instructions,
            deps=deps,
            model_settings=model_settings,
            usage_limits=usage_limits,
            usage=usage,
            metadata=metadata,
            output_retries=output_retries,
            infer_name=infer_name,
            toolsets=toolsets,
            builtin_tools=builtin_tools,
            # The tentacle's own capabilities ride every plain run; only
            # `subagent_run` mounts a caller-chosen set instead.
            capabilities=[*self.capabilities, *(capabilities or [])],
            spec=spec,
        ):
            if isinstance(event, AgentRunResultEvent):
                result = event.result
        if result is None:
            raise RuntimeError("react graph completed without an AgentRunResult")
        return result

    async def subagent_run(
        self,
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
    ) -> AgentRunResult[InklingOutput]:
        """The subagent contract, inkling-shaped: on top of the base policy
        (addressed conversation, non-interactive), the run mounts the tentacle's
        own capabilities and whatever its spawner adds — the same set a plain run
        gets. An accomplice calls the same tools and answers over the same
        ledgers, so what bounds one bounds the other; `interactive=False` is what
        makes the human-facing ones (ask, approvals) decline in-process rather
        than park a batch nothing resumes."""
        result: AgentRunResult[InklingOutput] | None = None
        async for event in self.iter_graph_events(
            user_prompt=user_prompt,
            conversation_address=conversation_address,
            thread_id=thread_id,
            conversation_id=conversation_id,
            interactive=False,
            run_name=run_name,
            model=model,
            effort=effort,
            instructions=instructions,
            capabilities=[*self.capabilities, *(capabilities or [])],
        ):
            if isinstance(event, AgentRunResultEvent):
                result = event.result
        if result is None:
            raise RuntimeError("react graph completed without an AgentRunResult")
        return result

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
        instructions: AgentInstructions[None] = None,
        deps: None = None,
        model_settings: AgentModelSettings[None] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[None] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[None]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[None]] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> ReactEventStream[InklingOutput]: ...

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
        instructions: AgentInstructions[None] = None,
        deps: None = None,
        model_settings: AgentModelSettings[None] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[None] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[None]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[None]] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> ReactEventStream[RunOutputDataT]: ...

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
        instructions: AgentInstructions[None] = None,
        deps: None = None,
        model_settings: AgentModelSettings[None] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[None] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[None]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[None]] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> ReactEventStream[InklingOutput | RunOutputDataT]:
        return ReactEventStream(
            self.iter_graph_events(
                user_prompt=user_prompt,
                conversation_address=conversation_address,
                thread_id=thread_id,
                source_thread_address=source_thread_address,
                source_thread_message_ids=source_thread_message_ids,
                run_name=run_name,
                output_type=output_type,
                deferred_tool_results=deferred_tool_results,
                deferred_suspender=deferred_suspender,
                model=model,
                effort=effort,
                conversation_id=conversation_id,
                interactive=interactive,
                instructions=instructions,
                deps=deps,
                model_settings=model_settings,
                usage_limits=usage_limits,
                usage=usage,
                metadata=metadata,
                output_retries=output_retries,
                infer_name=infer_name,
                toolsets=toolsets,
                builtin_tools=builtin_tools,
                capabilities=[*self.capabilities, *(capabilities or [])],
                spec=spec,
            )
        )

    async def iter_graph_events(
        self,
        *,
        user_prompt: str | Sequence[UserContent] | None,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None,
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
        instructions: AgentInstructions[None] = None,
        deps: None = None,
        model_settings: AgentModelSettings[None] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[None] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[None]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[None]] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AsyncGenerator[
        ReactStreamEvent[InklingOutput | RunOutputDataT],
        None,
    ]:
        resolved_run_name = run_name or "react"
        # Effort IS pydantic-ai's `thinking` scale; pass it through, layered over
        # any run settings the caller passed. Nothing downgrades a grade the
        # provider lacks — an OpenAI-compatible model sends the level on as
        # `reasoning_effort` verbatim — so a route's `efforts` claim has to
        # advertise only what its provider actually takes.
        if effort is not None:
            if callable(model_settings):
                raise ValueError(
                    "effort cannot be combined with callable model_settings"
                )
            model_settings = merge_model_settings(
                model_settings, ModelSettings(thinking=effort)
            )
        react_output_type: OutputSpec[InklingOutput | RunOutputDataT] = (
            [str, list[MessageSegment], DeferredToolRequests]
            if output_type is None
            else output_type
        )

        capabilities = list(capabilities or [])
        # The react graph carries only the thread/agent identity; each node fetches
        # the live conversation (and its history) from the ConversationManager, the
        # single source of truth — no message-history copy is threaded here. A
        # conversation is owned by a thread, so the run needs one.
        if thread_id is None:
            raise ValueError("agent run requires a thread_id to own its conversation")

        # Which deferrals this run answers itself is `InklingDeferrals`' decision, made
        # again at each one; all the run contributes is whether a human exists at all.
        # Which capability set to mount is likewise the entrypoints' decision, so
        # `capabilities` arrives here holding it. The run's project is the one thing
        # added below: it comes off the thread rather than the caller, so every
        # entrypoint would otherwise have to resolve it identically.
        project = await self.run_project(thread_id)
        if project is not None:
            capabilities.extend(self.project_capabilities(project))
        graph_deps = ReactDeps(
            agent=self.agent,
            conversation_manager=self.conversation_manager,
            thread_manager=self.octomate.thread_manager,
            agent_deps=deps,
            choose_resolvers=InklingDeferrals(
                interactive=interactive,
                configured=self.permission_mode,
                fallback=self.deferred_resolver,
            ),
            suspender=deferred_suspender,
            output_type=react_output_type,
            run_name=resolved_run_name,
            # Where this run happened, for the run to record: its project's root, or
            # nothing, since inkling has no configured directory to fall back to.
            cwd=project.root if project is not None else None,
            model=model,
            instructions=instructions,
            model_settings=model_settings,
            usage_limits=usage_limits or UsageLimits(request_limit=self.request_limit),
            usage=usage,
            metadata=metadata,
            output_retries=output_retries,
            infer_name=infer_name,
            toolsets=toolsets,
            builtin_tools=builtin_tools,
            capabilities=capabilities,
            spec=spec,
        )
        state = ReactState(
            conversation_address=conversation_address,
            agent_tentacle_id=self.id,
            thread_id=thread_id,
            conversation_id=conversation_id,
            source_thread_address=source_thread_address,
            source_thread_message_ids=list(source_thread_message_ids or []),
        )
        start_node = (
            ResumeTurn(deferred_results=deferred_tool_results)
            if deferred_tool_results is not None
            else StartTurn(user_prompt=user_prompt)
        )
        with inkling_logfire.span(
            "AgentTentacle {agent_id} {run_name} [{conversation_address}]",
            agent_id=self.id,
            run_name=resolved_run_name,
            conversation_address=str(conversation_address),
        ):
            async for event in iter_react_graph_events(
                start_node,
                state=state,
                deps=graph_deps,
            ):
                yield event
