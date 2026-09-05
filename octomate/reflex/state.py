"""Run-wide context every reflex node reads and writes.

The state and deps objects a node is handed, plus the two result variants a run
ends in. Split from the nodes so a node module imports what it operates on
without importing its siblings.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, TypeAlias, TypeVar, overload

from pydantic_ai import AgentRunResult
from pydantic_ai.messages import UserContent
from pydantic_ai.tools import DeferredToolRequests
from pydantic_graph import BaseNode

from octomate.config.agents import AgentRouteModelName
from octomate.config.channels import AgentModelConfig
from octomate.managers.conversation import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.managers.gateway import GatewayManager, GatewaySession
from octomate.managers.thread import ThreadManager
from octomate.managers.workspaces import WorkspaceManager
from octomate.prompts import tagged
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.thread import Thread, ThreadMessage
from octomate.schemas.triage import (
    AgentRoute,
    ResponseTargetMode,
    RunName,
    SummonDecision,
)
from octomate.schemas.user import UserProfile
from octomate.tentacles.agent import AgentTentacle
from octomate.tentacles.channel import (
    ChannelOutput,
    ChannelTentacle,
    ThreadStrategy,
)

# Inside the marking, where an editor puts its own: the tag says this is not the
# ask, and the sentence says why — an agent that answers the recap replies to what
# was settled hours ago.
RECAP_HEADER = (
    "Recent messages in this chat. They are context and have been answered "
    "already — do not reply to them."
)


@dataclass(frozen=True)
class ResponseTarget:
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
    is actually mounted — so React reads it there when it records the row, and
    nothing here could name it without risking a second, drifting copy.

    The source is who handed the conversation over, and where from as the row names
    it: the conversation the deciding turn ran in, the last model message in it and
    the run that left that message, read off the ledger rather than the run result
    because the row's foreign keys hold only for what was persisted. A native summon
    names its agent and nothing else, since the gateway cannot name the terminal
    session it came from; a route that pins a thread's owner names nobody."""

    source_agent_tentacle_id: str | None = None
    source_conversation_id: uuid.UUID | None = None
    source_run_id: str | None = None
    source_model_message_id: uuid.UUID | None = None


@dataclass
class ReflexResult:
    decision: SummonDecision | None
    target: ResponseTarget
    result: AgentRunResult[ChannelOutput] | None = None


@dataclass
class DeferredResult:
    requests: DeferredToolRequests
    target: ResponseTarget
    # The name of the run that deferred — carried for observability. `str`, not
    # `RunName`, because on a re-present it is read back from the persisted batch.
    run_name: str
    result: AgentRunResult[Any]
    batch_id: uuid.UUID | None = None


ReflexGraphResult: TypeAlias = ReflexResult | DeferredResult
# The node a reflex graph is entered at — see `build_reflex_graph`.
ReflexEntryT = TypeVar(
    "ReflexEntryT", bound="BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]"
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
    targets: dict[str, ResponseTarget] = field(default_factory=dict)
    summon_routes: list[AgentRoute] = field(default_factory=list)
    thread: Thread | None = None
    trigger_thread_message_id: uuid.UUID | None = None
    source_thread_address: ChannelAddress | None = None
    source_thread_message_ids: list[uuid.UUID] = field(default_factory=list)
    # The handoff the next React records when it lands, or None when it records
    # none.
    handoff: PendingHandoff | None = None
    user_prompt: str | Sequence[UserContent] | None = None
    user_profile: UserProfile | None = None


@dataclass
class ReflexDeps:
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

    def agent_configs(self, channel_id: str) -> list[AgentModelConfig]:
        return [
            agent_config
            for agent_config in self.channel(channel_id).config.agents
            if agent_config.agent in self.agents
        ]

    @cached_property
    def gateway_agents(self) -> frozenset[str]:
        """The agents whose driven turns offer the gateway spells: each agent's own
        flag, settled at startup and read once. A turn of any other agent builds no
        session, so external callers find nothing live for it."""
        return frozenset(id for id, agent in self.agents.items() if agent.gateway)

    async def gateway_session(
        self,
        agent: AgentTentacle,
        *,
        user_profile: UserProfile | None,
        thread_id: uuid.UUID | None,
        conversation_address: ChannelAddress,
    ) -> GatewaySession | None:
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
        session = GatewaySession(
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
        if thread_id is not None:
            # The same (thread, agent) key the run resolves internally, so an
            # external runtime's tool call finds this turn's session by the
            # conversation it already knows.
            conversation = await self.conversation_manager.ensure(
                thread_id, agent_tentacle_id=agent.id
            )
            session.conversation_id = conversation.id
        return session

    @cached_property
    def available_routes(self) -> dict[str, list[AgentRoute]]:
        return self.gateway.available_routes(self.channels, self.agents)

    def resolve_agent(
        self,
        channel_id: str,
        agent_id: str | None,
        model: AgentRouteModelName | None,
    ) -> AgentModelConfig:
        configs = self.agent_configs(channel_id)
        matched: AgentModelConfig | None = None
        for agent_config in configs:
            if agent_config.agent == agent_id and agent_config.model == model:
                return agent_config
            if agent_id is not None and agent_config.agent == agent_id:
                matched = agent_config
        if (
            matched is not None
            and model is not None
            and model in self.agent(matched.agent).models
        ):
            # Summonable models are claims-driven, not bounded by the channel's
            # entry list — honor any model the agent actually serves rather
            # than snapping back to the entry default.
            return AgentModelConfig(agent=matched.agent, model=model)
        return matched or configs[0]

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
