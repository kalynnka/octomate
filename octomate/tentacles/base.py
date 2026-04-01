from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, runtime_checkable

import anyio
from pydantic_ai import (
    Agent,
    AgentRunResult,
    CallDeferred,
    DeferredToolResults,
    RunContext,
)
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import FunctionToolset

from octomate.agents.base import SessionContext, SummonRequest
from octomate.agents.manager import SKILL_METADATA_KEY
from octomate.agents.tools import history_toolset
from octomate.schemas.actions import AgentMessage
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import (
    AgentSegment,
    ImageSegment,
    MarkdownSegment,
    ReplySegment,
    TextSegment,
)
from octomate.schemas.session import SessionKey, UserProfile
from octomate.stores.interaction import InteractionStore
from octomate.stores.thread import ThreadStore
from octomate.tentacles.feelers import NULL_FEELERS, Feelers

# agents.flick imports agents.surge which imports SendTarget from this module,
# and octopus imports Tentacle from this module — both circular.
if TYPE_CHECKING:
    from octomate.memory.base import OctopusMemory
    from octomate.octopus import Octopus

logger = logging.getLogger(__name__)


@dataclass
class PlatformMessage:
    msg_type: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Chromo(Protocol):
    """Two-way translation between platform-native wire format and internal schema."""

    async def sip(self, raw: Any) -> MessageEvent | None: ...

    async def squirt(
        self, segments: list[AgentSegment], *, reply_to: str | None = None
    ) -> list[PlatformMessage]: ...


@dataclass
class SendTarget:
    chat_type: Literal["group", "private"]
    chat_id: int | str
    reply_to: int | str | None = None
    reply_in_thread: bool = False


@runtime_checkable
class Ink(Protocol):
    """Structural protocol for platform API clients — identity and media only."""

    def inspect(self) -> UserProfile: ...

    async def get_user_profile(self, user_id: str) -> UserProfile: ...

    async def upload_media(self, data: bytes) -> str | None:
        """Upload media bytes, return a platform key/URL or None on failure."""
        ...

    async def download_media(
        self, resource_id: str, **kwargs: Any
    ) -> tuple[bytes, str] | None:
        """Download media by platform resource ID. Returns (data, filename) or None."""
        ...


class StreamSink:
    """Streaming output sink for agent tentacles.

    Channels provide platform-specific implementations via open_stream().
    """

    """Default StreamSink that sends each append/status as a separate message."""

    tentacle: ChannelTentacle
    target: SendTarget

    def __init__(self, tentacle: ChannelTentacle, target: SendTarget) -> None:
        self.tentacle = tentacle
        self.target = target

    async def set_status(self, status: str) -> None:
        await self.tentacle.twitch(self.target, [TextSegment(data={"text": status})])

    async def append(self, text: str) -> None:
        await self.tentacle.twitch(self.target, [MarkdownSegment(data={"text": text})])

    async def flush(self) -> None:
        pass

    async def post_thinking_block(self, text: str) -> None:
        """Post a collapsible thinking block.

        Default: appends the raw text so platforms without folding support
        still surface the content inline.
        """
        if text:
            await self.append(text)


class Tentacle(ABC):
    """Base for all tentacles — any bidirectional message channel."""

    id: str
    octopus: Octopus

    def __init__(self, id: str, octopus: Octopus) -> None:
        self.id = id
        self.octopus = octopus

    @abstractmethod
    async def __call__(
        self,
        key: SessionKey,
        contents: list[MessageEvent],
    ): ...


class AgentTentacle(Tentacle):
    """A tentacle wrapping an agent. On-demand, not long-running.
    Borrows the calling ChannelTentacle's feelers and ink for user interaction.

    Only one run per session key at a time. If a new message arrives while the
    agent is running, the current run is cancelled and re-started with the new
    message. Pending tool calls, questions, and todos from the previous run are
    dismissed in parallel.
    """

    description: str = ""
    handover: bool = False
    _running_tasks: dict[SessionKey, asyncio.Task]

    def __init__(
        self,
        id: str,
        octopus: Octopus,
        description: str = "",
        *,
        handover: bool = False,
    ) -> None:
        super().__init__(id, octopus)
        self.description = description
        self.handover = handover
        self._running_tasks = {}

    async def __call__(
        self,
        key: SessionKey,
        contents: list[MessageEvent],
    ) -> None:
        existing = self._running_tasks.get(key)
        if existing is not None and not existing.done():
            await self.interrupt(key)
            existing.cancel()
            asyncio.create_task(self._dismiss_pending(key))

        self._running_tasks[key] = asyncio.current_task()  # type: ignore[assignment]
        try:
            await self.run(key, contents)
        except asyncio.CancelledError:
            logger.info("AgentTentacle %s: run cancelled for [%s]", self.id, key)
            raise
        except Exception:
            logger.exception("Error in agent tentacle %s [%s]", self.id, key)
        finally:
            if self._running_tasks.get(key) is asyncio.current_task():
                self._running_tasks.pop(key, None)

    async def interrupt(self, key: SessionKey) -> None:
        """Send a graceful stop signal before hard-cancelling.

        Override in subclasses to e.g. send ClaudeSDKClient.interrupt().
        """

    @abstractmethod
    async def run(
        self,
        key: SessionKey,
        contents: list[MessageEvent],
    ) -> None: ...

    async def _dismiss_pending(self, key: SessionKey) -> None:
        """Background task: expire all pending interactions and dismiss their cards."""
        try:
            channel = self.octopus.tentacles.get(key.tentacle_id)
            if channel is None:
                return
            thread = await channel.threads.get(key)
            thread_id = str(thread.id)
            target = (
                SendTarget("group", key.group_id, reply_to=key.thread_id)
                if key.group_id
                else SendTarget("private", key.user_id, reply_to=key.thread_id)
            )
            (
                expired_confs,
                expired_qs,
            ) = await channel.interactions.expire_thread_interactions(thread_id)
            dismiss_coros = [
                channel.feelers.confirm.dismiss_confirmation(target, c)
                for c in expired_confs
            ] + [
                channel.feelers.questions.dismiss_question(target, q)
                for q in expired_qs
            ]
            if dismiss_coros:
                await asyncio.gather(*dismiss_coros, return_exceptions=True)
        except Exception:
            logger.exception(
                "AgentTentacle %s: error dismissing pending for [%s]", self.id, key
            )


class ChannelTentacle(Tentacle):
    """A tentacle wrapping a single IM platform connection. It receives events
    from the IM, resolves media, pushes events into the Nerve, and listens
    for outbound actions to send back."""

    FILES_ROOT: ClassVar[Path] = Path(".octomate/files")

    profile: UserProfile
    ink: Ink
    chromo: Chromo
    feelers: Feelers
    flick: Agent[SessionContext, list[AgentMessage] | DeferredToolRequests]
    memory: OctopusMemory
    buffer: MessageBuffer
    user_profiles: dict[str, UserProfile]
    threads: ThreadStore
    interactions: InteractionStore

    def __init__(
        self,
        id: str,
        octopus: Octopus,
        flick: Agent[SessionContext, list[AgentMessage] | DeferredToolRequests],
        memory: OctopusMemory,
        flush_delay: float = 0.5,
    ) -> None:
        super().__init__(id, octopus)
        self.flick = flick
        self.memory = memory
        self.buffer = MessageBuffer(flush_delay=flush_delay, handler=octopus.kick)
        self.user_profiles = {}
        self.threads = ThreadStore(default_owner=id)
        self.interactions = InteractionStore(threads=self.threads)
        self.feelers = NULL_FEELERS

        self.profile = self.ink.inspect()
        logger.info(
            "Tentacle %s: probed as %s (%s)",
            self.id,
            self.profile.user_id,
            self.profile.name,
        )

    @property
    def name(self) -> str:
        return self.profile.name

    @abstractmethod
    async def activate(self) -> None: ...

    @abstractmethod
    async def deactivate(self) -> None: ...

    async def __call__(self, key: SessionKey, contents: list[MessageEvent]) -> None:
        """Process an inbound message batch: run flick, handle summon or send response."""
        if not contents:
            return
        try:
            await self.memory.record(key, contents)

            if key.group_id and not any(
                msg.is_at(self.profile.user_id) for msg in contents
            ):
                await self.memory.memo(key, contents, self)
                return

            context = f"[group: {key.group_id}]" if key.group_id else "[chat: private]"
            header = f"[me: {self.name} ({self.profile.user_id})] {context}"
            user_prompt: list = [header]
            for msg in contents:
                user_prompt.extend(msg.to_content_parts())

            memories = await self.memory.recall(key, contents, self)
            if memories:
                facts = "\n".join(f"- {m}" for m in memories)
                instructions = f"[relevant memories]\n{facts}"
            else:
                instructions = None

            logger.info(
                "Tentacle %s flick [%s] (%d messages)", self.id, key, len(contents)
            )

            target = (
                SendTarget("group", key.group_id)
                if key.group_id
                else SendTarget("private", key.user_id)
            )
            target.reply_to = key.thread_id
            deps = SessionContext(session_key=key, tentacle=self)
            message_history = await self.memory.history(key, size=32)
            result = await self.flick.run(
                user_prompt,
                deps=deps,
                toolsets=self.toolsets,
                instructions=instructions,
                message_history=message_history,
            )

            resolved = await self.resolve_deferred(
                agent=self.flick,
                result=result,
                deps=deps,
                key=key,
                target=target,
            )

            if isinstance(resolved, SummonRequest):
                agent_tentacle = self.octopus.agent_tentacles.get(resolved.tentacle_tag)
                if agent_tentacle is None:
                    logger.warning(
                        "Tentacle %s: unknown agent tentacle id %r",
                        self.id,
                        resolved.tentacle_tag,
                    )
                else:
                    content = contents[-1].model_copy()
                    content.segments = [TextSegment(data={"text": resolved.summary})]
                    if agent_tentacle.handover:
                        await self.threads.set_owner(key, agent_tentacle)
                        await self.twitch(
                            target,
                            [
                                TextSegment(
                                    data={
                                        "text": f"Tentacle *{agent_tentacle.id}* has grabbed this thread 🐙!"
                                    }
                                )
                            ],
                        )
                    asyncio.create_task(agent_tentacle(key, [content]))
            elif resolved:
                for msg_out in resolved.output:
                    await self.twitch(target, msg_out.segments)
                asyncio.create_task(self.memory.memo(key, resolved.output, self))
        except Exception:
            logger.exception("Error in tentacle %s [%s]", self.id, key)

    async def resolve_deferred(
        self,
        agent: Agent[SessionContext, list[AgentMessage] | DeferredToolRequests],
        result: AgentRunResult[list[AgentMessage] | DeferredToolRequests],
        key: SessionKey,
        deps: SessionContext,
        target: SendTarget,
    ) -> AgentRunResult[list[AgentMessage]] | SummonRequest | None:
        """Resolve deferred tool requests until the agent produces final messages.

        Returns the final result with list[AgentMessage] output,
        a SummonRequest if the agent summoned an agent tentacle,
        or None if resolution failed.
        """
        while isinstance(result.output, DeferredToolRequests):
            # Check for summon — abort immediately, extract args from the deferred call
            summon_call = next(
                (c for c in result.output.calls if c.tool_name == "summon"), None
            )
            if summon_call:
                args = summon_call.args_as_dict()
                return SummonRequest(
                    tentacle_tag=args.get("tentacle_tag", ""),
                    summary=args.get("summary", ""),
                    user_prefer=args.get("user_prefer", ""),
                    language=args.get("language", ""),
                )

            deferred = DeferredToolResults()

            for call in result.output.calls:
                if call.tool_name == "ask_user":
                    args = call.args_as_dict()
                    resp = await self.feelers.questions.ask_question(
                        target,
                        args.get("question", ""),
                        session_key=key,
                        options=args.get("options"),
                    )
                    deferred.calls[call.tool_call_id] = (
                        resp.answer if resp else "(no response)"
                    )

            for call in result.output.approvals:
                tool_meta = result.output.metadata.get(call.tool_call_id, {})

                # Check session allowlist — skip confirmation if already allowed
                thread = await self.threads.get(key)
                if self.feelers.confirm.is_session_allowed(
                    str(thread.id), call.tool_name
                ):
                    deferred.approvals[call.tool_call_id] = True
                    continue

                action, future = await self.feelers.confirm.create_confirmation(
                    key=key,
                    tool_name=call.tool_name,
                    tool_call_id=call.tool_call_id,
                    args=call.args_as_dict(),
                    title=tool_meta.get("description", call.tool_name),
                    description=tool_meta.get("description", ""),
                    skill=tool_meta.get(SKILL_METADATA_KEY, ""),
                    approvers=tool_meta.get("approvers"),
                )

                sent = await self.feelers.confirm.send_confirmation(target, action)
                if not sent:
                    await self.feelers.confirm.expire_confirmation(
                        action.confirmation_id
                    )
                    deferred.approvals[call.tool_call_id] = False
                    continue

                try:
                    approved = await asyncio.wait_for(
                        future, timeout=self.feelers.confirm.timeout
                    )
                except TimeoutError:
                    await self.feelers.confirm.expire_confirmation(
                        action.confirmation_id
                    )
                    await self.feelers.confirm.send_timeout_notification(target, action)
                    approved = False
                except asyncio.CancelledError:
                    await self.feelers.confirm.expire_confirmation(
                        action.confirmation_id
                    )
                    await self.feelers.confirm.send_timeout_notification(target, action)
                    raise

                deferred.approvals[call.tool_call_id] = approved

            result = await agent.run(
                message_history=result.all_messages(),
                deferred_tool_results=deferred,
                deps=deps,
                toolsets=self.toolsets,
            )

        return result  # type: ignore[return-value]

    async def ingest(self, raw: Any) -> None:
        """Inbound pipeline: decode → enrich sender → resolve media → triage."""
        try:
            event = await self.chromo.sip(raw)
            if event is None:
                return
            event.tentacle_id = self.id
            event.self_id = self.profile.user_id
            event.sender = await self.get_user_profile(event.user_id)
            await self.submerge(event)
            self.buffer.push(event)
        except Exception:
            logger.exception("Tentacle %s: error in ingest", self.id)

    async def twitch(self, target: SendTarget, segments: list[AgentSegment]) -> None:
        """Outbound pipeline: resolve media → encode → send."""
        await self.emerge(segments)
        reply_to: str | None = str(target.reply_to) if target.reply_to else None
        for seg in segments:
            if isinstance(seg, ReplySegment):
                reply_to = seg.data["id"]
                break
        remaining: list[AgentSegment] = [
            s for s in segments if not isinstance(s, ReplySegment)
        ]
        messages: list[PlatformMessage] = await self.chromo.squirt(remaining)
        await self.send_platform_message(
            str(target.chat_id),
            target.chat_type,
            messages,
            reply_to,
            target.reply_in_thread,
        )

    @abstractmethod
    async def send_platform_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[PlatformMessage],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str | None: ...

    @asynccontextmanager
    async def open_stream(self, target: SendTarget) -> AsyncIterator[StreamSink]:
        """Open a streaming sink for progressive output.

        Subclasses override to provide platform-specific streaming (e.g. Slack
        message updates). The default sends each append/status as a separate message.
        """
        sink = StreamSink(tentacle=self, target=target)
        try:
            yield sink
        finally:
            await sink.flush()

    async def get_user_profile(self, user_id: str) -> UserProfile:
        cached = self.user_profiles.get(user_id)
        if cached is not None:
            return cached
        profile = await self.ink.get_user_profile(user_id)
        self.user_profiles[user_id] = profile
        return profile

    async def submerge(self, event: MessageEvent) -> None:
        """Resolve inbound media: download images from the event to local storage."""
        pending = [seg for seg in event.segments if isinstance(seg, ImageSegment)]
        if not pending:
            return
        save = self.den(event)
        message_id = str(event.message_id)
        await anyio.Path(save).mkdir(parents=True, exist_ok=True)
        async with asyncio.TaskGroup() as tg:
            for seg in pending:
                tg.create_task(self.absorb(seg, save, message_id))

    async def emerge(self, segments: list[AgentSegment]) -> None:
        """Prepare outbound media: upload images before sending."""
        for seg in segments:
            if isinstance(seg, ImageSegment):
                await self.secrete(seg)

    @abstractmethod
    async def absorb(self, seg: ImageSegment, save_dir: Path, message_id: str) -> None:
        """Download a single inbound image to save_dir."""
        ...

    @abstractmethod
    async def secrete(self, seg: ImageSegment) -> None:
        """Prepare a single outbound image for sending."""
        ...

    def den(self, event: MessageEvent) -> Path:
        subdir = event.chat_id if event.chat_type == "group" else event.user_id
        return self.FILES_ROOT / self.id / subdir

    @cached_property
    def toolsets(self) -> list[FunctionToolset[SessionContext]]:
        toolset = FunctionToolset[SessionContext]()

        @toolset.tool
        async def acknowledge(ctx: RunContext[SessionContext], text: str) -> str:
            """
            Send a short message to the user immediately before doing heavy work or
            provide extra info ahead of next steps to inform the user what is going on.

            Call this FIRST when about to invoke a skill or tool that may take a few
            seconds (e.g. weather, search, knowledge base), so the user knows you are
            working on it. Do NOT use for simple replies like greetings or responses
            that don't involve tool calls. Example: acknowledge("let me look that up~")
            """
            if ctx.deps.tentacle:
                key = ctx.deps.session_key
                if key.group_id:
                    target = SendTarget("group", key.group_id, reply_to=key.thread_id)
                else:
                    target = SendTarget("private", key.user_id, reply_to=key.thread_id)
                # Flick always replies in the main thread — sub-thread messages
                # are routed directly to agent tentacles and never reach here.
                await ctx.deps.tentacle.twitch(
                    target, [TextSegment(data={"text": text})]
                )
            return "acknowledged"

        @toolset.tool
        async def ask_user(
            ctx: RunContext[SessionContext],
            question: str,
            options: list[str] | None = None,
        ) -> str:
            """Ask the user a question and wait for their answer before continuing.

            ALWAYS USE this tool when you need clarification or a decision from the user.
            DO NOT SEND a separate text message asking the same thing — this tool handles it.
            Provide options to show choice buttons; omit for free-text input
            (platform-dependent — may not be supported everywhere).
            Returns the user's answer, or '(no response)' on timeout.
            """
            raise CallDeferred()

        @toolset.tool
        async def create_todo(ctx: RunContext[SessionContext], title: str) -> str:
            """Create a TODO card for the user in the current chat.

            Use this whenever a task has multiple stages or steps — create a todo item
            for each stage so the user can track progress. Returns a todo ID on success,
            or an error message if not supported on this platform.
            """
            if not ctx.deps.tentacle:
                return "not supported"
            key = ctx.deps.session_key
            item = await ctx.deps.tentacle.feelers.todos.create_todo(key, title)
            return f"todo:{item.todo_id}" if item else "not supported on this platform"

        return [toolset, history_toolset()]


class MessageBuffer:
    _flush_delay: float
    _handler: Callable[[SessionKey, list[MessageEvent]], Awaitable[None]]
    _buckets: defaultdict[SessionKey, list[MessageEvent]]
    _pending: set[SessionKey]

    def __init__(
        self,
        flush_delay: float,
        handler: Callable[[SessionKey, list[MessageEvent]], Awaitable[None]],
    ) -> None:
        self._flush_delay = flush_delay
        self._handler = handler
        self._buckets: defaultdict[SessionKey, list[MessageEvent]] = defaultdict(list)
        self._pending: set[SessionKey] = set()

    def push(self, event: MessageEvent) -> None:
        key = event.session_key
        self._buckets[key].append(event)
        if key not in self._pending:
            self._pending.add(key)
            asyncio.create_task(self._flush_after_delay(key))

    async def _flush_after_delay(self, key: SessionKey) -> None:
        await asyncio.sleep(self._flush_delay)
        self._pending.discard(key)
        batch = self._buckets.pop(key, [])
        if not batch:
            return
        try:
            await self._handler(key, batch)
        except Exception:
            logger.exception("Error handling batch for %s", key)
