from __future__ import annotations

import asyncio
import logging
import os

from anyio import create_memory_object_stream as object_stream
from anyio.abc import ObjectReceiveStream, ObjectSendStream
from pydantic_ai import Agent, AgentRunResult, DeferredToolResults
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.tools import DeferredToolApprovalResult, DeferredToolRequests

from octomate.agents import SessionContext, create_companion_agent
from octomate.agents.manager import SkillManager
from octomate.config import MindConfig
from octomate.memory.base import OctopusMemory
from octomate.schemas.actions import AgentMessage
from octomate.schemas.events import GroupMessageEvent, MessageEvent
from octomate.schemas.session import SessionKey
from octomate.store import ConfirmationStore
from octomate.tentacles.base import SendTarget, Tentacle

logger = logging.getLogger(__name__)


class Octopus:
    agent: Agent[SessionContext, list[AgentMessage] | DeferredToolRequests]
    confirmations: ConfirmationStore
    memory: OctopusMemory
    tentacles: dict[str, Tentacle]
    _nerve_send: ObjectSendStream[tuple[SessionKey, list[MessageEvent]]]
    _nerve_receive: ObjectReceiveStream[tuple[SessionKey, list[MessageEvent]]]

    def __init__(
        self,
        brain: MindConfig,
        memory: OctopusMemory,
        skill_manager: SkillManager | None = None,
        buffer_size: int = 64,
    ) -> None:
        self.tentacles = {}
        self.confirmations = ConfirmationStore()
        self.memory = memory
        self._nerve_send, self._nerve_receive = object_stream(buffer_size)
        if brain.base_url:
            os.environ.setdefault("GOOGLE_GEMINI_BASE_URL", brain.base_url)
        self.agent = create_companion_agent(brain, skill_manager)

    async def activate(self) -> None:
        try:
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
            self.memory.save()

    def connect(self, tentacle: Tentacle) -> None:
        if tentacle.tag in self.tentacles:
            raise ValueError(f"Tentacle {tentacle.tag!r} already connected")
        self.tentacles[tentacle.tag] = tentacle
        logger.info("Connected tentacle: %s", tentacle.tag)

    def cut(self, name: str) -> None:
        self.tentacles.pop(name, None)

    async def confirm(self, confirmation_id: str, approved: bool) -> bool:
        return self.confirmations.resolve(confirmation_id, approved)

    async def kick(self, key: SessionKey, batch: list[MessageEvent]) -> None:
        await self._nerve_send.send((key, batch))

    async def rolling(self) -> None:
        async with asyncio.TaskGroup() as tg, self._nerve_receive:
            async for key, batch in self._nerve_receive:
                tg.create_task(self.think(key, batch))

    async def think(self, key: SessionKey, batch: list[MessageEvent]) -> None:
        try:
            await self._think(key, batch)
        except Exception:
            logger.exception("Error processing batch [%s]", key)

    async def _think(self, key: SessionKey, batch: list[MessageEvent]) -> None:
        if not batch:
            return

        tentacle = self.tentacles[key.tentacle_id]
        profile = tentacle.profile

        if key.group_id is not None and not any(
            isinstance(msg, GroupMessageEvent) and msg.is_at(tentacle.id)
            for msg in batch
        ):
            # record and memo messages but skip thinking
            self.memory.record(
                key,
                [
                    ModelRequest(parts=[UserPromptPart(content=str(msg))])
                    for msg in batch
                ],
            )
            await self.memory.memo(key, batch, tentacle)
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

        history = self.memory.history(key)
        self.memory.record(
            key,
            [ModelRequest(parts=[UserPromptPart(content=str(msg))]) for msg in batch],
        )

        user_prompt: list = [header]
        for msg in batch:
            user_prompt.extend(msg.to_content_parts())

        memories = await self.memory.recall(key, batch, tentacle)
        if memories:
            facts = "\n".join(f"- {m}" for m in memories)
            content = f"[relevant memories]\n{facts}"
            history.append(ModelRequest(parts=[UserPromptPart(content=content)]))

        logger.info("Octopus processing batch [%s] (%d messages)", key, len(batch))

        if key.group_id is not None:
            target = SendTarget("group", key.group_id)
        else:
            target = SendTarget("private", key.user_id)

        deps = SessionContext(session_key=key, tentacle=tentacle)
        result = await self.agent.run(
            user_prompt,
            message_history=history,
            deps=deps,
        )

        if isinstance(result.output, DeferredToolRequests):
            await self.handle_deferred(
                result=result,  # type: ignore[arg-type]
                deps=deps,
                key=key,
                target=target,
                tentacle=tentacle,
            )
        else:
            self.memory.record(key, result.new_messages())
            logger.info("Agent returned %d messages for [%s]", len(result.output), key)

            for msg in result.output:
                await tentacle.twitch(target, msg.segments)

            asyncio.create_task(self.memory.memo(key, result.output, tentacle))

    async def handle_deferred(
        self,
        result: AgentRunResult[DeferredToolRequests],
        key: SessionKey,
        deps: SessionContext,
        target: SendTarget,
        tentacle: Tentacle,
    ) -> DeferredToolResults:
        approvals: dict[str, bool | DeferredToolApprovalResult] = {}

        for call in result.output.approvals:
            action, future = self.confirmations.create(
                session_key=key,
                tool_name=call.tool_name,
                tool_call_id=call.tool_call_id,
                args=call.args_as_dict(),
                description=result.output.metadata.get(call.tool_call_id, {}).get(
                    "description", ""
                ),
            )

            sent = await tentacle.send_confirmation(target, action)
            if not sent:
                self.confirmations.expire(action.confirmation_id)
                approvals[call.tool_call_id] = False
                continue

            try:
                approved = await asyncio.wait_for(
                    future, timeout=self.confirmations.timeout
                )
            except TimeoutError:
                self.confirmations.expire(action.confirmation_id)
                approved = False

            approvals[call.tool_call_id] = approved

        return await self.agent.run(
            message_history=result.all_messages(),
            deferred_tool_results=DeferredToolResults(approvals=approvals),
            deps=deps,
        )  # type: ignore[return-value]
