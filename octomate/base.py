from __future__ import annotations

import logging

import anyio
from pydantic_ai import Agent

from octomate.config import BrainConfig
from octomate.nerve import OctopusNerve
from octomate.schemas.events import (
    MessageEvent,
    MessageSegment,
    SendGroupMsgAction,
    SendGroupMsgParams,
    SendPrivateMsgAction,
    SendPrivateMsgParams,
    SessionKey,
    TextSegment,
)
from octomate.tentacles.base import BaseTentacle

logger = logging.getLogger(__name__)


class Octopus:
    def __init__(self, nerve: OctopusNerve, brain: BrainConfig) -> None:
        self.nerve = nerve
        self._agent = Agent(
            brain.model,
            system_prompt=brain.system_prompt,
        )

    def connect(self, tentacle: BaseTentacle) -> None:
        self.nerve.connect(tentacle)

    async def activate(self) -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(self.nerve.activate)
            tg.start_soon(self._consume_inbound)

    async def _consume_inbound(self) -> None:
        async with self.nerve.inbound:
            async for key, batch in self.nerve.inbound:
                await self._handle_batch(key, batch)

    async def _handle_batch(self, key: SessionKey, batch: list[MessageEvent]) -> None:
        prompt = self._format_batch(batch)
        logger.debug("Brain processing batch [%s] (%d messages)", key, len(batch))

        result = await self._agent.run(prompt)
        reply_text = result.output

        segments: list[MessageSegment] = [TextSegment(data={"text": reply_text})]

        if key.group_id is not None:
            action = SendGroupMsgAction(
                tentacle_id=key.tentacle_id,
                params=SendGroupMsgParams(
                    group_id=key.group_id,
                    message=segments,
                ),
            )
        else:
            action = SendPrivateMsgAction(
                tentacle_id=key.tentacle_id,
                params=SendPrivateMsgParams(
                    user_id=key.user_id,
                    message=segments,
                ),
            )

        await self.nerve.pulse(action)

    @staticmethod
    def _format_batch(batch: list[MessageEvent]) -> str:
        lines: list[str] = []
        for msg in batch:
            name = msg.sender.nickname
            text = msg.text_content()
            if text:
                lines.append(f"{name}: {text}")
        return "\n".join(lines)
