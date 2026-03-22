from __future__ import annotations

import asyncio
import contextlib
import logging

from anyio import create_memory_object_stream as object_stream
from anyio.abc import ObjectReceiveStream, ObjectSendStream
from pydantic_ai import Agent, AgentRunResult, DeferredToolResults
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.tools import DeferredToolRequests

from octomate.agents.base import SessionContext
from octomate.agents.manager import SkillManager
from octomate.schemas.actions import AgentMessage
from octomate.schemas.events import MessageEvent
from octomate.schemas.session import SessionKey
from octomate.store import InteractionStore
from octomate.tentacles.base import SendTarget, Tentacle

logger = logging.getLogger(__name__)


class Octopus:
    agent: Agent[SessionContext, list[AgentMessage] | DeferredToolRequests]
    store: InteractionStore
    skill_manager: SkillManager | None
    tentacles: dict[str, Tentacle]
    _nerve_send: ObjectSendStream[tuple[SessionKey, list[MessageEvent]]]
    _nerve_receive: ObjectReceiveStream[tuple[SessionKey, list[MessageEvent]]]

    def __init__(
        self,
        agent: Agent[SessionContext, list[AgentMessage] | DeferredToolRequests],
        skill_manager: SkillManager | None = None,
        buffer_size: int = 64,
    ) -> None:
        self.agent = agent
        self.tentacles = {}
        self.store = InteractionStore()
        self.skill_manager = skill_manager
        self._nerve_send, self._nerve_receive = object_stream(buffer_size)

    async def activate(self) -> None:
        try:
            async with contextlib.AsyncExitStack() as stack:
                if self.skill_manager:
                    await stack.enter_async_context(self.skill_manager)
                async with asyncio.TaskGroup() as tg:
                    for name, tentacle in self.tentacles.items():
                        tg.create_task(tentacle.activate(), name=f"tentacle:{name}")
                    tg.create_task(self.rolling())
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.exception("Fatal error in task group", exc_info=exc)
        finally:
            for tentacle in self.tentacles.values():
                await tentacle.deactivate()
                tentacle.memory.save()

    def connect(self, tentacle: Tentacle) -> None:
        if tentacle.tag in self.tentacles:
            raise ValueError(f"Tentacle {tentacle.tag!r} already connected")
        self.tentacles[tentacle.tag] = tentacle
        logger.info("Connected tentacle: %s", tentacle.tag)

    def cut(self, name: str) -> None:
        self.tentacles.pop(name, None)

    async def kick(self, key: SessionKey, batch: list[MessageEvent]) -> None:
        await self._nerve_send.send((key, batch))

    async def rolling(self) -> None:
        async with asyncio.TaskGroup() as tg, self._nerve_receive:
            async for key, batch in self._nerve_receive:
                tg.create_task(self.flick(key, batch))

    async def flick(self, key: SessionKey, batch: list[MessageEvent]) -> None:
        """Direct path: full message history + semantic recall. Has the silent group-message guard."""
        if not batch:
            return
        try:
            tentacle = self.tentacles[key.tentacle_id]
            profile = tentacle.profile

            if key.group_id is not None and not any(
                msg.is_at(tentacle.id) for msg in batch
            ):
                tentacle.memory.record(
                    key,
                    [
                        ModelRequest(parts=[UserPromptPart(content=str(msg))])
                        for msg in batch
                    ],
                )
                await tentacle.memory.memo(key, batch, tentacle)
                return

            header = (
                f"[me: {profile.name} ({profile.user_id})]"
                + " "
                + (
                    f"[group: {key.group_id}]"
                    if key.group_id is not None
                    else "[chat: private]"
                )
            )

            history = tentacle.memory.history(key)
            tentacle.memory.record(
                key,
                [
                    ModelRequest(parts=[UserPromptPart(content=str(msg))])
                    for msg in batch
                ],
            )

            user_prompt: list = [header]
            for msg in batch:
                user_prompt.extend(msg.to_content_parts())

            memories = await tentacle.memory.recall(key, batch, tentacle)
            if memories:
                facts = "\n".join(f"- {m}" for m in memories)
                history.append(
                    ModelRequest(
                        parts=[UserPromptPart(content=f"[relevant memories]\n{facts}")]
                    )
                )

            logger.info("Octopus flick [%s] (%d messages)", key, len(batch))

            target = (
                SendTarget("group", key.group_id)
                if key.group_id is not None
                else SendTarget("private", key.user_id)
            )
            deps = SessionContext(session_key=key, tentacle=tentacle)
            result = await tentacle.flick.run(
                user_prompt,
                message_history=history,
                deps=deps,
                toolsets=tentacle.toolsets,
            )

            if deps.handed_over:
                return

            if isinstance(result.output, DeferredToolRequests):
                result = await self.handle_deferred(
                    agent=tentacle.flick,
                    result=result,  # type: ignore[arg-type]
                    deps=deps,
                    key=key,
                    target=target,
                    tentacle=tentacle,
                )

            if isinstance(result.output, list):
                tentacle.memory.record(key, result.new_messages())
                logger.info(
                    "Flick returned %d messages for [%s]", len(result.output), key
                )
                for msg in result.output:
                    await tentacle.twitch(target, msg.segments)
                asyncio.create_task(tentacle.memory.memo(key, result.output, tentacle))
        except Exception:
            logger.exception("Error in flick [%s]", key)

    async def surge(self, key: SessionKey, *, summary: str) -> None:
        """Handover path: summary only. No raw message history, no memory."""
        try:
            tentacle = self.tentacles[key.tentacle_id]
            profile = tentacle.profile

            header = (
                f"[me: {profile.name} ({profile.user_id})]"
                + " "
                + (
                    f"[group: {key.group_id}]"
                    if key.group_id is not None
                    else "[chat: private]"
                )
            )

            user_prompt: list = [header, f"[summary]\n{summary}"]

            logger.info("Octopus surge [%s]", key)

            target = (
                SendTarget("group", key.group_id)
                if key.group_id is not None
                else SendTarget("private", key.user_id)
            )
            deps = SessionContext(session_key=key, tentacle=tentacle)
            result = await self.agent.run(
                user_prompt,
                toolsets=tentacle.toolsets,
                deps=deps,
            )

            if isinstance(result.output, DeferredToolRequests):
                result = await self.handle_deferred(
                    agent=self.agent,
                    result=result,  # type: ignore[arg-type]
                    deps=deps,
                    key=key,
                    target=target,
                    tentacle=tentacle,
                )

            if isinstance(result.output, list):
                logger.info(
                    "Surge returned %d messages for [%s]", len(result.output), key
                )
                for msg in result.output:
                    await tentacle.twitch(target, msg.segments)
        except Exception:
            logger.exception("Error in surge [%s]", key)

    async def handle_deferred(
        self,
        agent: Agent[SessionContext, list[AgentMessage] | DeferredToolRequests],
        result: AgentRunResult[DeferredToolRequests],
        key: SessionKey,
        deps: SessionContext,
        target: SendTarget,
        tentacle: Tentacle,
    ) -> AgentRunResult[list[AgentMessage] | DeferredToolRequests]:
        deferred = DeferredToolResults()

        for call in result.output.calls:
            if call.tool_name == "ask_user":
                args = call.args_as_dict()
                resp = await tentacle.feelers.questions.ask_question(
                    target, args.get("question", ""), args.get("options")
                )
                deferred.calls[call.tool_call_id] = (
                    resp.answer if resp else "(no response)"
                )

        for call in result.output.approvals:
            tool_meta = result.output.metadata.get(call.tool_call_id, {})
            action, future = self.store.create_confirmation(
                session_key=key,
                tool_name=call.tool_name,
                tool_call_id=call.tool_call_id,
                args=call.args_as_dict(),
                description=tool_meta.get("description", ""),
                approvers=tool_meta.get("approvers"),
            )

            sent = await tentacle.send_confirmation(target, action)
            if not sent:
                self.store.expire_confirmation(action.confirmation_id)
                deferred.approvals[call.tool_call_id] = False
                continue

            try:
                approved = await asyncio.wait_for(future, timeout=self.store.timeout)
            except TimeoutError:
                self.store.expire_confirmation(action.confirmation_id)
                approved = False

            deferred.approvals[call.tool_call_id] = approved

        return await agent.run(
            message_history=result.all_messages(),
            deferred_tool_results=deferred,
            deps=deps,
            toolsets=tentacle.toolsets,
        )
