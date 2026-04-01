from __future__ import annotations

import asyncio
import contextlib
import logging

from octomate.agents.manager import SkillManager
from octomate.nerve import (
    AgentSignal,
    AnyioNerve,
    AskUser,
    ChannelSignal,
    ConfirmResult,
    ConfirmTool,
    DismissPending,
    MessageBatch,
    Nerve,
    NerveDispatcher,
    PendingRequests,
    ReleaseThread,
    SendSegments,
    StreamFrame,
    SummonAgent,
    TodoResult,
    TodoUpdate,
    UserAnswer,
)
from octomate.schemas.events import MessageEvent
from octomate.schemas.session import SessionKey
from octomate.tentacles.base import AgentTentacle, ChannelTentacle, SendTarget, StreamSink
from octomate.transmuters import sqlalchemy_materia

logger = logging.getLogger(__name__)


class Octopus:
    skill_manager: SkillManager | None
    tentacles: dict[str, ChannelTentacle]
    agent_tentacles: dict[str, AgentTentacle]
    channel_nerve: Nerve[ChannelSignal]
    channel_dispatcher: NerveDispatcher[ChannelSignal]
    agent_nerve: Nerve[AgentSignal]
    agent_dispatcher: NerveDispatcher[AgentSignal]
    answer_pending: PendingRequests[UserAnswer]
    confirm_pending: PendingRequests[ConfirmResult]
    todo_pending: PendingRequests[TodoResult]
    _sinks: dict[SessionKey, StreamSink]
    _sink_stacks: dict[SessionKey, contextlib.AsyncExitStack]
    _octopus_todo_ts: dict[SessionKey, str]

    def __init__(
        self,
        skill_manager: SkillManager | None = None,
        buffer_size: int = 64,
    ) -> None:
        self.tentacles = {}
        self.agent_tentacles = {}
        self.skill_manager = skill_manager
        self.channel_nerve = AnyioNerve(buffer_size)
        self.channel_dispatcher = NerveDispatcher(self.channel_nerve)
        self.agent_nerve = AnyioNerve(buffer_size)
        self.agent_dispatcher = NerveDispatcher(self.agent_nerve)
        self.answer_pending = PendingRequests()
        self.confirm_pending = PendingRequests()
        self.todo_pending = PendingRequests()
        self._sinks = {}
        self._sink_stacks = {}
        self._octopus_todo_ts = {}

        @self.channel_dispatcher.on(MessageBatch)
        async def handle_message_batch(signal: MessageBatch) -> None:
            channel = self.tentacles[signal.key.tentacle_id]
            owner_id = await channel.threads.get_owner(signal.key)
            owner = self.agent_tentacles.get(owner_id)
            if owner:
                await owner(signal.key, signal.events)
            else:
                await channel.threads.set_owner(signal.key, channel)
                await channel(signal.key, signal.events)

        @self.agent_dispatcher.on(SummonAgent)
        async def handle_summon_agent(signal: SummonAgent) -> None:
            agent = self.agent_tentacles.get(signal.agent_tag)
            if agent is None:
                logger.warning("Octopus: unknown agent tentacle id %r", signal.agent_tag)
                return
            channel = self.tentacles.get(signal.key.tentacle_id)
            if channel is not None:
                await channel.threads.set_owner(signal.key, agent)
            else:
                logger.warning(
                    "Octopus: SummonAgent channel %r not found, ownership not set",
                    signal.key.tentacle_id,
                )
            asyncio.create_task(agent(signal.key, signal.contents))

        @self.agent_dispatcher.on(StreamFrame)
        async def handle_stream_frame(signal: StreamFrame) -> None:
            channel = self.tentacles.get(signal.key.tentacle_id)
            if channel is None:
                return
            key = signal.key
            if signal.frame_type == "close":
                stack = self._sink_stacks.pop(key, None)
                if stack:
                    await stack.aclose()
                self._sinks.pop(key, None)
                return
            if key not in self._sinks:
                target = _target_from_key(key)
                stack = contextlib.AsyncExitStack()
                sink = await stack.enter_async_context(channel.open_stream(target))
                self._sink_stacks[key] = stack
                self._sinks[key] = sink
            sink = self._sinks[key]
            if signal.frame_type == "status":
                await sink.set_status(signal.content)
            elif signal.frame_type == "append":
                await sink.append(signal.content)
            elif signal.frame_type == "thinking":
                await sink.post_thinking_block(signal.content)
            elif signal.frame_type == "flush":
                await sink.flush()

        @self.agent_dispatcher.on(SendSegments)
        async def handle_send_segments(signal: SendSegments) -> None:
            channel = self.tentacles.get(signal.key.tentacle_id)
            if channel is None:
                return
            await channel.twitch(signal.target, signal.segments)

        @self.agent_dispatcher.on(AskUser)
        async def handle_ask_user(signal: AskUser) -> None:
            channel = self.tentacles.get(signal.key.tentacle_id)
            if channel is None:
                await self.agent_nerve.send(
                    UserAnswer(request_id=signal.request_id, answer="(no response)")
                )
                return
            key = signal.key
            target = _target_from_key(key)
            resp = await channel.feelers.questions.ask_question(
                target,
                signal.question,
                session_key=key,
                options=signal.options or None,
                multi_select=signal.multi_select,
            )
            answer = resp.answer if resp else "(no response)"
            await self.agent_nerve.send(
                UserAnswer(request_id=signal.request_id, answer=answer)
            )

        @self.agent_dispatcher.on(UserAnswer)
        async def handle_user_answer(signal: UserAnswer) -> None:
            self.answer_pending.resolve(signal.request_id, signal)

        @self.agent_dispatcher.on(ConfirmTool)
        async def handle_confirm_tool(signal: ConfirmTool) -> None:
            channel = self.tentacles.get(signal.key.tentacle_id)
            if channel is None:
                await self.agent_nerve.send(
                    ConfirmResult(request_id=signal.request_id, approved=False)
                )
                return
            key = signal.key
            target = _target_from_key(key)
            thread = await channel.threads.get(key)
            if channel.feelers.confirm.is_session_allowed(str(thread.id), signal.tool_name):
                await self.agent_nerve.send(
                    ConfirmResult(request_id=signal.request_id, approved=True)
                )
                return
            action, future = await channel.feelers.confirm.create_confirmation(
                key=key,
                tool_name=signal.tool_name,
                tool_call_id="",
                args=signal.args,
                title=signal.title,
                description=signal.description,
                skill=signal.skill,
                approvers=signal.approvers or None,
            )
            sent = await channel.feelers.confirm.send_confirmation(target, action)
            if not sent:
                await channel.feelers.confirm.expire_confirmation(action.confirmation_id)
                await self.agent_nerve.send(
                    ConfirmResult(request_id=signal.request_id, approved=False)
                )
                return
            try:
                approved = await asyncio.wait_for(
                    future, timeout=channel.feelers.confirm.timeout
                )
            except TimeoutError:
                await channel.feelers.confirm.expire_confirmation(action.confirmation_id)
                await channel.feelers.confirm.send_timeout_notification(target, action)
                approved = False
            except asyncio.CancelledError:
                await channel.feelers.confirm.expire_confirmation(action.confirmation_id)
                await channel.feelers.confirm.send_timeout_notification(target, action)
                raise
            await self.agent_nerve.send(
                ConfirmResult(request_id=signal.request_id, approved=approved)
            )

        @self.agent_dispatcher.on(ConfirmResult)
        async def handle_confirm_result(signal: ConfirmResult) -> None:
            self.confirm_pending.resolve(signal.request_id, signal)

        @self.agent_dispatcher.on(TodoUpdate)
        async def handle_todo_update(signal: TodoUpdate) -> None:
            channel = self.tentacles.get(signal.key.tentacle_id)
            if channel is None:
                await self.agent_nerve.send(TodoResult(key=signal.key, ts=""))
                return
            key = signal.key
            target = _target_from_key(key)
            existing_ts = self._octopus_todo_ts.get(key)
            new_ts = await channel.feelers.todos.upsert_todo_list(
                target, signal.items, existing_ts=existing_ts
            )
            if new_ts:
                if new_ts != existing_ts:
                    pinned = await channel.feelers.todos.pin_todo(target, new_ts)
                    if not pinned:
                        logger.warning(
                            "handle_todo_update: failed to pin todo card ts=%s", new_ts
                        )
                self._octopus_todo_ts[key] = new_ts
            await self.agent_nerve.send(TodoResult(key=key, ts=new_ts or ""))

        @self.agent_dispatcher.on(TodoResult)
        async def handle_todo_result(signal: TodoResult) -> None:
            self.todo_pending.resolve(str(signal.key), signal)

        @self.agent_dispatcher.on(DismissPending)
        async def handle_dismiss_pending(signal: DismissPending) -> None:
            key = signal.key
            channel = self.tentacles.get(key.tentacle_id)
            if channel is None:
                return
            target = _target_from_key(key)
            todo_ts = self._octopus_todo_ts.pop(key, None)
            if todo_ts:
                await channel.feelers.todos.unpin_todo(target, todo_ts)
            stack = self._sink_stacks.pop(key, None)
            if stack:
                await stack.aclose()
            self._sinks.pop(key, None)
            thread = await channel.threads.get(key)
            thread_id = str(thread.id)
            expired_confs, expired_qs = await channel.interactions.expire_thread_interactions(
                thread_id
            )
            dismiss_coros = [
                channel.feelers.confirm.dismiss_confirmation(target, c)
                for c in expired_confs
            ] + [
                channel.feelers.questions.dismiss_question(target, q)
                for q in expired_qs
            ]
            if dismiss_coros:
                await asyncio.gather(*dismiss_coros, return_exceptions=True)

        @self.agent_dispatcher.on(ReleaseThread)
        async def handle_release_thread(signal: ReleaseThread) -> None:
            channel = self.tentacles.get(signal.key.tentacle_id)
            if channel is not None:
                await channel.threads.set_owner(signal.key, channel)

    async def activate(self) -> None:
        try:
            async with contextlib.AsyncExitStack() as stack:
                stack.enter_context(sqlalchemy_materia)
                if self.skill_manager:
                    await stack.enter_async_context(self.skill_manager)
                async with asyncio.TaskGroup() as tg:
                    for name, tentacle in self.tentacles.items():
                        tg.create_task(tentacle.activate(), name=f"tentacle:{name}")
                    tg.create_task(self.channel_dispatcher.run())
                    tg.create_task(self.agent_dispatcher.run())
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.exception("Fatal error in task group", exc_info=exc)
        finally:
            for tentacle in self.tentacles.values():
                await tentacle.deactivate()

    def connect(self, tentacle: ChannelTentacle) -> None:
        if tentacle.id in self.tentacles:
            raise ValueError(f"Tentacle {tentacle.id!r} already connected")
        self.tentacles[tentacle.id] = tentacle
        logger.info("Connected tentacle: %s", tentacle.id)

    def graft(self, tentacle: AgentTentacle) -> None:
        if tentacle.id in self.agent_tentacles:
            raise ValueError(f"Agent tentacle {tentacle.id!r} already grafted")
        self.agent_tentacles[tentacle.id] = tentacle
        logger.info("Grafted agent tentacle: %s", tentacle.id)

    def cut(self, name: str) -> None:
        self.tentacles.pop(name, None)

    async def kick(self, key: SessionKey, batch: list[MessageEvent]) -> None:
        await self.channel_nerve.send(MessageBatch(key=key, events=batch))


def _target_from_key(key: SessionKey) -> SendTarget:
    if key.group_id:
        return SendTarget("group", key.group_id, reply_to=key.thread_id)
    return SendTarget("private", key.user_id, reply_to=key.thread_id)
