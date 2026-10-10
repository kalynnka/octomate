"""Graph state, dependencies and runtime orchestration.

The state and deps objects a node is handed, the runtime shared by execution
entries, and the result variants a run ends in.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any, TypeVar, overload

from opentelemetry import trace
from pydantic import UUID7
from pydantic_ai import AgentCapability, AgentRunResult, AgentRunResultEvent
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import UserContent
from pydantic_ai.tools import DeferredToolRequests
from pydantic_graph import BaseNode, End, GraphRunContext

from octomate.capabilities.gateway import GatewayCapability
from octomate.capabilities.harness.events import (
    GatewayEvent,
    MessageSentEvent,
    RunErrorEvent,
    RunStartedEvent,
)
from octomate.capabilities.harness.react import ReactStreamEvent
from octomate.config.agents import AgentRouteModelName
from octomate.config.channels import AgentModelConfig
from octomate.managers.conversation import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.managers.gateway import GatewayManager, OctomateSession
from octomate.managers.thread import ThreadManager
from octomate.managers.workspaces import WorkspaceManager
from octomate.prompts import tagged
from octomate.reflex.suspender import ReflexSuspender
from octomate.schemas.commands import CommandError, CommandOutcome
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MarkdownSegment, MessageSegment, TextSegment
from octomate.schemas.thread import Thread, ThreadCommand, ThreadMessage
from octomate.schemas.triage import (
    AgentRoute,
    AgentRouteKey,
    ResponseTargetMode,
    RunName,
    SchemeDecision,
    SummonDecision,
)
from octomate.schemas.user import UserProfile
from octomate.telemetry import reflex_logfire
from octomate.tentacles.agent import AgentTentacle
from octomate.tentacles.channel import (
    ChannelOutput,
    ChannelTentacle,
    ThreadStrategy,
)
from octomate.tentacles.feelers.output import split_reply

if TYPE_CHECKING:
    from octomate.reflex.nodes.scheme import Scheme
    from octomate.reflex.nodes.summon import Summon
    from octomate.reflex.nodes.teleport import Teleport
from octomate.tentacles.feelers.output import IMMessageID

logger = logging.getLogger(__name__)

# Inside the marking, where an editor puts its own: the tag says this is not the
# ask, and the sentence says why — an agent that answers the recap replies to what
# was settled hours ago.
RECAP_HEADER = (
    "Recent messages in this chat. They are context and have been answered "
    "already — do not reply to them."
)


@dataclass(frozen=True)
class ResponseTarget:
    """Where a turn answers: the channel, the address on it, and how the reply is
    placed."""

    channel_id: str
    address: ChannelAddress | None = None
    # Routing only — how an inbound threaded message is handled (`Route`). What this
    # channel can actually open lives on the channel itself, as `surfaces`.
    thread_strategy: ThreadStrategy = "main_only"
    mode: ResponseTargetMode = "main"

    def __str__(self) -> str:
        chat_type = self.address.chat_type if self.address else "unresolved"
        return (
            f"- {self.channel_id}: chat_type={chat_type}, mode={self.mode}, "
            f"thread_strategy={self.thread_strategy}"
        )


@dataclass(frozen=True)
class PendingHandoff:
    """A handoff one node decided, as far as it is known before it lands: the source
    side. The target is what landing resolves — the decision's route, as the agent
    is actually mounted — so it is read there when the row is recorded, and
    nothing here could name it without risking a second, drifting copy.

    The source is who handed the conversation over, and where from as the row names
    it: the conversation the deciding turn ran in, the last model message in it and
    the run that left that message, read off the ledger rather than the run result
    because the row's foreign keys hold only for what was persisted. A native summon
    names its agent and nothing else, since the gateway cannot name the terminal
    session it came from; a route that pins a thread's owner names nobody."""

    source_agent_tentacle_id: str | None = None
    source_conversation_id: UUID7 | None = None
    source_run_id: str | None = None
    source_model_message_id: UUID7 | None = None

    async def land(
        self, deps: ReflexDeps, thread: Thread, decision: SummonDecision
    ) -> None:
        """Record this handoff on the chat `thread` belongs to, naming the agent
        and model `decision` resolved to, unless that chat already names them."""
        target_conversation = await deps.conversation_manager.ensure(
            thread.id, agent_tentacle_id=decision.agent_id, with_history=False
        )
        # A handoff pins who owns the chat, so it is read and written there: a
        # chat room's sub-thread is new every kick and would forget the owner.
        chat = await deps.thread_manager.surface(thread)
        latest = chat.latest_handoff
        if (
            latest is not None
            and latest.to_agent_tentacle_id == decision.agent_id
            and latest.to_model == decision.model
        ):
            return
        await deps.thread_manager.record_handoff(
            chat,
            source_agent_tentacle_id=self.source_agent_tentacle_id,
            to_agent_tentacle_id=decision.agent_id,
            to_model=decision.model,
            reason=decision.reason,
            hint=decision.hint,
            brief=decision.brief,
            source_conversation_id=self.source_conversation_id,
            target_conversation_id=target_conversation.id,
            source_run_id=self.source_run_id,
            source_model_message_id=self.source_model_message_id,
        )


@dataclass
class ReflexResult:
    """A finished turn: the decision it ran on, where it answered, and the agent's
    result."""

    decision: SummonDecision | None
    target: ResponseTarget
    result: AgentRunResult[ChannelOutput] | None = None


@dataclass
class DeferredResult:
    """A turn that stopped on deferred requests, and the batch they were parked
    as."""

    requests: DeferredToolRequests
    target: ResponseTarget
    # The name of the run that deferred — carried for observability. `str`, not
    # `RunName`, because on a re-present it is read back from the persisted batch.
    run_name: str
    result: AgentRunResult[Any]
    batch_id: UUID7 | None = None


type ReflexGraphResult = ReflexResult | DeferredResult | CommandOutcome
# The node a reflex graph is entered at — see `build_reflex_graph`.
ReflexEntryT = TypeVar(
    "ReflexEntryT",
    bound="BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]",
)


@dataclass
class ReflexState:
    """All run-wide context for one reflex graph run.

    Awake resolves the source context once and writes it here; downstream nodes
    read from state and carry only transition discriminators.
    """

    source_target: ResponseTarget | None = None
    target: ResponseTarget | None = None
    run_name: RunName = "react"
    decision: SummonDecision | None = None
    selection: AgentRouteKey | None = None  # Selected agent and model.
    conversation_id: uuid.UUID | None = None  # Current invocation's conversation.
    targets: dict[str, ResponseTarget] = field(default_factory=dict)
    summon_routes: list[AgentRoute] = field(default_factory=list)
    thread: Thread | None = None
    trigger_thread_message_id: UUID7 | None = None
    source_thread_address: ChannelAddress | None = None
    source_thread_message_ids: list[UUID7] = field(default_factory=list)
    # The handoff the next React records when it lands, or None when it records
    # none.
    handoff: PendingHandoff | None = None
    user_prompt: str | Sequence[UserContent] | None = None
    user_profile: UserProfile | None = None
    # The channel an operation on another channel's thread was performed in, which
    # hears how the turn goes; None when the turn came from the source's channel.
    operated_from: str | None = None


@dataclass
class ReflexDeps:
    """The host's registries and managers a reflex run reaches through."""

    channels: dict[str, ChannelTentacle]
    # No defaults for the managers: a deps object must carry the host's own — the
    # ledger, the conversations, the deferred actions, the gateway's live-session
    # registry — never private ones with their own identity or state.
    thread_manager: ThreadManager
    # For the bookkeeping a finished turn owes and the agent has no part in:
    # leaving the thread's workspace somewhere losing the directory cannot cost it.
    workspaces: WorkspaceManager
    conversation_manager: ConversationManager
    action_manager: DeferredActionManager
    gateway: GatewayManager
    agents: dict[str, AgentTentacle] = field(default_factory=dict)

    @cached_property
    def runtime(self) -> ReflexRuntime:
        """Stateless orchestration shared by this graph's execution entries."""
        return ReflexRuntime()

    @overload
    def channel(self, target: ResponseTarget) -> ChannelTentacle: ...

    @overload
    def channel(self, target: str) -> ChannelTentacle: ...

    def channel(self, target: ResponseTarget | str) -> ChannelTentacle:
        channel_id = target.channel_id if isinstance(target, ResponseTarget) else target
        channel = self.channels.get(channel_id)
        if channel is None:
            raise ValueError(f"unknown channel {channel_id!r}")
        return channel

    def agent(self, agent_id: str) -> AgentTentacle:
        agent = self.agents.get(agent_id)
        if agent is None:
            raise ValueError(f"unknown agent {agent_id!r}")
        return agent

    @cached_property
    def gateway_agents(self) -> frozenset[str]:
        """The agents whose driven turns offer the gateway spells: each agent's own
        flag, settled at startup and read once. A turn of any other agent builds no
        session, so external callers find nothing live for it."""
        return frozenset(id for id, agent in self.agents.items() if agent.gateway)

    async def octomate_session(
        self,
        agent: AgentTentacle,
        *,
        user_profile: UserProfile | None,
        thread_id: UUID7 | None,
        conversation_address: ChannelAddress,
        conversation_id: uuid.UUID | None = None,
    ) -> OctomateSession | None:
        """One turn's gateway for `agent`, or None for an agent whose flag is off.

        Built from the host's own registries, every channel's and not just this
        one's: a spell that crosses lands where another channel's config decides who
        runs, so the gateway offers — and checks against — that channel's routes,
        and reads `surfaces` off it to know whether `scheme` can land. The agents
        are what the accomplice spells run; without a thread there is nowhere for
        a child conversation to live, and the gateway then does not offer them.
        """
        if agent.id not in self.gateway_agents:
            return None
        session = OctomateSession(
            channel_routes=self.available_routes,
            current_agent_id=agent.id,
            channels=self.channels,
            users=self.thread_manager.users,
            user_profile=user_profile,
            agents=self.agents,
            thread_id=thread_id,
            conversation_address=conversation_address,
            threads=self.thread_manager,
            workspaces=self.workspaces,
        )
        if conversation_id is not None:
            session.conversation_id = conversation_id
        elif thread_id is not None:
            # The same (thread, agent) key the run resolves internally, so an
            # external runtime's tool call finds this turn's session by the
            # conversation it already knows.
            conversation = await self.conversation_manager.ensure(
                thread_id, agent_tentacle_id=agent.id, with_history=False
            )
            session.conversation_id = conversation.id
        return session

    @property
    def available_routes(self) -> dict[str, list[AgentRoute]]:
        return self.gateway.available_routes(self.channels, self.agents)

    def resolve_agent(
        self,
        channel_id: str,
        agent_id: str | None,
        model: AgentRouteModelName | None,
    ) -> AgentModelConfig:
        """Select an exposed agent and resolve a model from its catalog."""
        agent_ids = self.channel(channel_id).agent_ids
        if agent_id is None:
            agent_id = agent_ids[0]
        if agent_id not in agent_ids:
            raise ValueError(
                f"agent {agent_id!r} is not bound to channel {channel_id!r}"
            )

        agent = self.agent(agent_id)
        if not agent.models:
            raise ValueError(f"agent {agent_id!r} has no available model catalog")
        if model is None:
            return AgentModelConfig(agent=agent_id, model=agent.resolve_model())
        served = agent.served_model(model)
        if served is None:
            raise ValueError(f"agent {agent_id!r} does not serve model {model!r}")
        return AgentModelConfig(agent=agent_id, model=served)

    async def render_chat(
        self, messages: list[ThreadMessage], *, ceiling: int = 0
    ) -> str:
        """The ledger rows as one transcript: who spoke, under which identities, and
        the `#msg:<id>` handle a brief would cite them by.

        Each message is its segments, as they render themselves — the same body an
        inbound event shows. `message_text` would be the shorter route and the wrong
        one: it keeps only text and markdown, so a message that was a picture or a
        quoted reply arrives as nothing at all.

        `ceiling` cuts each message. It is 0 for the messages a turn must answer —
        those are the work — and set for the chat behind them, where one pasted log
        would otherwise be the whole slice.
        """
        parts: list[str] = []
        for message in messages:
            if isinstance(message, ThreadCommand):
                continue
            text = "\n".join(str(segment) for segment in message.segments)
            if not text:
                continue
            if ceiling and len(text) > ceiling:
                text = f"{text[:ceiling]}…"
            sender = await message.sender
            display_name = (
                (sender.name or sender.nickname or "anonymous")
                if sender is not None
                else "anonymous"
            )
            owner = sender.user.peek() if sender is not None else None
            ids = (
                f"{message.user_id}, user:{owner.username}"
                if owner is not None
                else message.user_id
            )
            platform_id = (
                f" #msg:{message.platform_message_id}"
                if message.platform_message_id
                else ""
            )
            parts.append(f"{display_name} ({ids}){platform_id}:\n{text}")
        return "\n\n".join(parts)

    async def announce(
        self, state: ReflexState, address: ChannelAddress, event: GatewayEvent
    ) -> IMMessageID | None:
        """Present a move where the conversation was, and in the channel it was
        operated from when that is another. Answers the platform id of the line it
        left."""
        operated_from = state.operated_from
        if operated_from is not None and operated_from != address.channel_tentacle_id:
            await self.channel(operated_from).feelers.present(address, event)
        # A native session's pseudo-channel has no feelers.
        channel = self.channels.get(address.channel_tentacle_id)
        return await channel.feelers.present(address, event) if channel else None

    async def report(self, state: ReflexState, error: Exception) -> None:
        """Tell the channel the turn came from, or the one it was operated from,
        that it failed. A turn that failed before it knew its source has nobody to
        tell."""
        source = state.source_target.address if state.source_target else None
        if source is None:
            return
        channel = self.channels.get(state.operated_from or source.channel_tentacle_id)
        if channel is None:
            return
        trace_id = format(trace.get_current_span().get_span_context().trace_id, "032x")
        try:
            await channel.feelers.present(
                source, RunErrorEvent(message=str(error), trace_id=trace_id)
            )
        except Exception:
            logger.warning(
                "Channel %s could not report a failed turn", channel.id, exc_info=True
            )

    async def record_move(
        self,
        address: ChannelAddress,
        text: str,
        *,
        agent_tentacle_id: str,
        platform_message_id: str | None = None,
    ) -> None:
        """Write the line a move leaves behind to the ledger of the chat it left.

        A person already sees it — the channel posted it, and a thread opened here
        hangs off it. The turn that comes next does not: a chat room's recap is built
        from the ledger, and a move nobody recorded leaves a chat in which the work
        simply stops. An agent reading that answers what was carried away ten minutes
        ago.

        The row carries the opener's own platform id, so the ledger and the platform
        name the same message rather than two.
        """
        await self.thread_manager.record_outbound(
            address,
            agent_tentacle_id=agent_tentacle_id,
            segments=[MarkdownSegment(data={"text": text})],
            sender=self.channel(address.channel_tentacle_id).self_profile,
            platform_message_id=platform_message_id,
            message_text=text,
            raw=text,
        )

    async def load_pending_prompt(
        self,
        state: ReflexState,
        active_agent_id: str,
    ) -> None:
        source_target = state.source_target
        if (
            state.thread is None
            or state.trigger_thread_message_id is None
            or source_target is None
            or source_target.address is None
        ):
            return
        # Pull every recorded chat-ledger row that has not been bound into a
        # model request yet: rule-gated group messages, sleeping/not-kicked
        # messages, and messages that stacked up behind an already-running turn.
        messages = await self.thread_manager.pending_prompt_messages(
            state.thread,
            state.trigger_thread_message_id,
            active_agent_id,
        )
        if not messages:
            return
        state.source_thread_address = source_target.address
        state.source_thread_message_ids = [message.id for message in messages]

        asked = await self.render_chat(messages)
        if not asked:
            return
        recap = await self.chat_recap(state, messages[0])
        # The messages the turn must answer are the body, unmarked. What was said
        # before them is the system's addition and stands in a tag.
        state.user_prompt = f"{recap}\n\n{asked}" if recap else asked

    async def chat_recap(self, state: ReflexState, earliest: ThreadMessage) -> str:
        """What was said in this chat room before the messages the turn must answer.

        Only a kick in a dm or a group chat needs it: that runs in a thread of its
        own, so its model context starts empty every time and the agent would answer
        a chat it cannot see. A thread carries its own history in the conversation
        and gets none of this — its context is already the whole of the work.

        The chat is the parent's, so the rows come from there; how many, and how much
        of each, is the channel's to say.
        """
        thread = state.thread
        source_target = state.source_target
        if thread is None or thread.parent_thread_id is None or source_target is None:
            return ""
        recap = self.channel(source_target).config.recap
        if not recap.messages:
            return ""
        messages = await self.thread_manager.chat_messages_before(
            thread.parent_thread_id, earliest.id, limit=recap.messages
        )
        rendered = await self.render_chat(messages, ceiling=recap.characters)
        return tagged("chat_recap", f"{RECAP_HEADER}\n\n{rendered}" if rendered else "")


class ReflexRuntime:
    """Stateless orchestration in graph deps; invocation resources stay with callers."""

    async def resources(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> tuple[OctomateSession | None, ReflexSuspender, list[AgentCapability[None]]]:
        """Build fresh gateway, approval and tool context for the selected invocation."""
        state = ctx.state
        target = state.target
        source = state.source_target
        selection = state.selection
        if (
            target is None
            or target.address is None
            or source is None
            or source.address is None
            or selection is None
        ):
            raise ValueError(
                "a Reflex run requires a selected agent and resolved source and target addresses"
            )
        agent = ctx.deps.agent(selection.agent_id)
        channel = ctx.deps.channel(target)
        thread_id = state.thread.id if state.thread else None
        state.summon_routes = [
            route
            for route in ctx.deps.available_routes[target.channel_id]
            if route.agent_id != agent.id
        ]
        if state.conversation_id is None and thread_id is not None:
            conversation = await ctx.deps.conversation_manager.ensure(
                thread_id, agent_tentacle_id=agent.id, with_history=False
            )
            state.conversation_id = conversation.id
        session = await ctx.deps.octomate_session(
            agent,
            user_profile=state.user_profile,
            thread_id=thread_id,
            conversation_address=target.address,
            conversation_id=state.conversation_id,
        )
        capabilities: list[AgentCapability[None]] = []
        if session is not None:
            capabilities.append(
                GatewayCapability(
                    session=session, conversations=ctx.deps.conversation_manager
                )
            )
        suspender = ReflexSuspender(
            channel=channel,
            action_manager=ctx.deps.action_manager,
            conversation_manager=ctx.deps.conversation_manager,
            agent_tentacle_id=agent.id,
            run_name=state.run_name,
            source_address=source.address,
            target_address=target.address,
            target_mode=target.mode,
            decision=state.decision,
            model=selection.model,
            thread_id=thread_id,
            emit_on_stream=channel.config.stream.enabled,
        )
        return session, suspender, capabilities

    async def prepare_user(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
        profile: UserProfile,
        *,
        session: OctomateSession | None,
        suspender: ReflexSuspender,
        capabilities: list[AgentCapability[None]],
    ) -> None:
        """Mount capabilities belonging to the user authorized for this invocation."""
        ctx.state.user_profile = profile
        if session is not None:
            session.user_profile = profile
        agent = ctx.deps.agent(suspender.agent_tentacle_id)
        capabilities.extend(await agent.user_capabilities(profile))

    async def send(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
        suspender: ReflexSuspender,
        event: MessageSentEvent,
    ) -> bool:
        """Deliver a gateway send away from the active timeline, when possible."""
        destination = event.destination
        if destination is None:
            return False
        channel = ctx.deps.channel(destination.channel_tentacle_id)
        dm = await channel.open_dm(destination.user_id)
        if dm is None:
            logger.warning(
                "Channel %s could not open a DM with %s; delivering the send to %s instead",
                destination.channel_tentacle_id,
                destination.user_id,
                suspender.target_address,
            )
            return False
        await channel.feelers.segments.present(dm, event.segments)
        await ctx.deps.thread_manager.record_outbound(
            dm,
            agent_tentacle_id=suspender.agent_tentacle_id,
            segments=event.segments,
            sender=channel.self_profile,
        )
        return True

    async def drive(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
        suspender: ReflexSuspender,
        source: AsyncGenerator[ReactStreamEvent[ChannelOutput], None],
    ) -> tuple[AgentRunResult[ChannelOutput], IMMessageID | None]:
        """Consume the same native invocation, closing it before recording its reply,
        and answer the platform id its timeline rendered the reply as."""
        results: list[AgentRunResult[ChannelOutput]] = []
        errors: list[Exception] = []
        presented: IMMessageID | None = None
        address = suspender.target_address

        async def events() -> AsyncGenerator[ReactStreamEvent[ChannelOutput], None]:
            yield RunStartedEvent(address=address)
            try:
                async for event in source:
                    if isinstance(event, AgentRunResultEvent):
                        results.append(event.result)
                    if isinstance(event, MessageSentEvent) and await self.send(
                        ctx, suspender, event
                    ):
                        continue
                    yield event
            except Exception as error:
                errors.append(error)
                raise

        async with aclosing(source):
            try:
                async with aclosing(events()) as stream:
                    presented = await self.present_events(suspender, stream)
            except AgentRunError:
                raise
            except Exception:
                logger.warning(
                    "Channel %s: timeline render failed",
                    suspender.channel.id,
                    exc_info=True,
                )
        if errors:
            raise errors[0]
        if not results:
            raise RuntimeError(f"react stream for {address} completed without a result")
        return results[-1], presented

    async def present_events(
        self,
        suspender: ReflexSuspender,
        stream: AsyncGenerator[ReactStreamEvent[ChannelOutput], None],
    ) -> IMMessageID | None:
        """Render a live timeline, or deliver explicit sends while draining a quiet
        run. Answers the platform id the timeline rendered the reply as."""
        address = suspender.target_address
        channel = suspender.channel
        if not channel.config.stream.enabled:
            async for event in stream:
                if isinstance(event, MessageSentEvent):
                    await channel.feelers.segments.present(address, event.segments)
            return None
        async with channel.feelers.timeline.open(address) as timeline:
            await timeline.drive(stream)
        return timeline.message_id

    async def present_result(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
        suspender: ReflexSuspender,
        result: AgentRunResult[ChannelOutput],
        *,
        streamed: bool,
        presented: IMMessageID | None = None,
    ) -> None:
        """Record and bind the reply, presenting it once when it was not streamed.
        `presented` is the platform id a streamed reply was rendered as."""
        output = result.output
        address = suspender.target_address
        channel = suspender.channel
        segments: list[MessageSegment]
        if isinstance(output, str):
            segments = [MarkdownSegment(data={"text": output})] if output else []
        elif isinstance(output, Iterable):
            segments = list(output)
        else:
            return
        _reply_to, body = split_reply(segments)
        message = None
        if body:
            message = await ctx.deps.thread_manager.record_outbound(
                address,
                agent_tentacle_id=suspender.agent_tentacle_id,
                segments=body,
                sender=channel.self_profile,
                raw=output
                if isinstance(output, str)
                else "\n\n".join(str(segment) for segment in body),
                message_text=output if isinstance(output, str) else None,
            )
        await ctx.deps.thread_manager.bind_assistant_replies(
            [message.id] if message is not None else [],
            run_id=result.run_id,
        )
        if streamed or (isinstance(output, str) and not output):
            message_id = presented
        elif isinstance(output, str):
            message_id = await channel.feelers.markdown.present(address, output)
        else:
            message_id = await channel.feelers.segments.present(address, segments)
        if message is not None:
            await ctx.deps.thread_manager.mark_presented(message, message_id)

    async def present_command(
        self, suspender: ReflexSuspender, outcome: CommandOutcome
    ) -> None:
        """Deliver direct feedback; its command receipt already owns the output."""
        segments: list[MessageSegment] = (
            [TextSegment(data={"text": outcome.message})]
            if isinstance(outcome, CommandError)
            else outcome.segments
        )
        if not segments:
            return
        address = suspender.target_address
        channel = suspender.channel
        if not channel.config.stream.enabled:
            await channel.feelers.segments.present(address, segments)
            return

        async def events() -> AsyncGenerator[ReactStreamEvent[ChannelOutput], None]:
            yield MessageSentEvent(segments=segments)

        async with (
            channel.feelers.timeline.open(address) as timeline,
            aclosing(events()) as stream,
        ):
            await timeline.drive(stream)

    async def finish(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
        session: OctomateSession | None,
        suspender: ReflexSuspender,
        run_result: AgentRunResult[ChannelOutput],
    ) -> Summon | Teleport | Scheme | End[ReflexGraphResult]:
        """Follow the gateway decision or deferral left by an actual agent run."""
        # These nodes import the graph types consumed by this runtime.
        from octomate.reflex.nodes.scheme import Scheme
        from octomate.reflex.nodes.summon import Summon
        from octomate.reflex.nodes.teleport import Teleport

        state = ctx.state
        target = state.target
        if target is None:
            raise ValueError("a Reflex run requires a resolved target")
        decision = state.decision
        agent_id = suspender.agent_tentacle_id
        thread_id = state.thread.id if state.thread else None
        output = run_result.output
        with reflex_logfire.span("reflex.finish", run_id=run_result.run_id) as span:
            if session is not None and session.dismissing and state.thread is not None:
                # The agent said this thread's work is done: its tree goes now
                # that the run is out of it, saved first and kept if that failed.
                result = await ctx.deps.workspaces.dismiss(state.thread)
                span.set_attribute(
                    "react.dismissed",
                    result,
                )
            if isinstance(output, DeferredToolRequests):
                # `teleport` is resolved by the graph (fork + resume), not a human. The
                # suspender classified it by its declared metadata kind and stashed it,
                # so route on the typed request instead of re-scanning tool names.
                if suspender.teleport is not None:
                    return Teleport(
                        request=suspender.teleport, origin=target, agent_id=agent_id
                    )
                return End(
                    DeferredResult(
                        requests=output,
                        target=target,
                        run_name=state.run_name,
                        result=run_result,
                        batch_id=suspender.suspended_batch_id,
                    )
                )

            gateway_decision = session.decision if session else None
            if isinstance(gateway_decision, SchemeDecision | SummonDecision):
                # Where this handoff came from, as the row records it: the
                # conversation this turn ran in, as of its last message.
                conversation = (
                    await ctx.deps.conversation_manager.ensure(
                        thread_id, agent_tentacle_id=agent_id
                    )
                    if thread_id is not None
                    else None
                )
                last = (
                    max(
                        conversation.messages,
                        key=lambda message: message.id,
                        default=None,
                    )
                    if conversation is not None
                    else None
                )
                state.handoff = PendingHandoff(
                    source_agent_tentacle_id=agent_id,
                    source_conversation_id=conversation.id if conversation else None,
                    source_run_id=last.run_id if last else None,
                    source_model_message_id=last.id if last else None,
                )
            if isinstance(gateway_decision, SchemeDecision):
                span.set_attribute("react.action", gateway_decision.action)
                reflex_logfire.info(
                    "react -> scheme into the asker's dm",
                    destination=str(gateway_decision.destination),
                )
                return Scheme(
                    request=gateway_decision,
                    origin=target,
                    agent_id=agent_id,
                )
            if isinstance(gateway_decision, SummonDecision):
                state.decision = gateway_decision
                state.target = target
                state.run_name = "summon"
                span.set_attribute("react.action", gateway_decision.action)
                span.set_attribute("react.next_agent_id", gateway_decision.agent_id)
                reflex_logfire.info(
                    "react -> {action} agent={agent_id}",
                    action=gateway_decision.action,
                    agent_id=gateway_decision.agent_id,
                    reason=gateway_decision.reason,
                )
                return Summon()

            return End(
                ReflexResult(
                    decision=decision,
                    target=target,
                    result=run_result,
                )
            )
