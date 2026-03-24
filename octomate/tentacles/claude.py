from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    TextBlock,
)
from claude_agent_sdk.types import HookContext, HookInput, SyncHookJSONOutput

from octomate.config import ClaudeCodeConfig
from octomate.schemas.segments import MarkdownData, MarkdownSegment
from octomate.schemas.session import SessionKey
from octomate.tentacles.base import AgentTentacle, ChannelTentacle, SendTarget

if TYPE_CHECKING:
    from octomate.octopus import Octopus

logger = logging.getLogger(__name__)


class ClaudeCodeTentacle(AgentTentacle):
    config: ClaudeCodeConfig

    def __init__(self, tag: str, octopus: Octopus, config: ClaudeCodeConfig) -> None:
        super().__init__(tag, octopus)
        self.config = config

    async def dispatch(self, task: str, channel: ChannelTentacle, target: SendTarget):
        session_key = SessionKey(
            tentacle_id=channel.tag,
            user_id=str(target.chat_id),
        )

        async def pre_tool_use(
            input_data: HookInput, tool_use_id: str | None, context: HookContext
        ) -> SyncHookJSONOutput:
            tool_name = input_data.get("tool_name", "")
            tool_input = input_data.get("tool_input", {})

            action, future = self.octopus.store.create_confirmation(
                session_key=session_key,
                tool_name=tool_name,
                tool_call_id=tool_use_id or "",
                args=tool_input if isinstance(tool_input, dict) else {},
                title=f"Claude Code: {tool_name}",
                description=_summarize(tool_name, tool_input),
                skill="claude_code",
            )

            sent = await channel.send_confirmation(target, action)
            if not sent:
                self.octopus.store.expire_confirmation(action.confirmation_id)
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "Could not deliver approval request",
                    }
                }

            try:
                approved = await asyncio.wait_for(
                    future, timeout=self.octopus.store.timeout
                )
            except TimeoutError:
                self.octopus.store.expire_confirmation(action.confirmation_id)
                approved = False

            if not approved:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "User denied",
                    }
                }
            return {}

        options = ClaudeAgentOptions(
            cwd=self.config.cwd,
            model=self.config.model or None,
            max_turns=self.config.max_turns,
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher="", hooks=[pre_tool_use]),
                ],
            },
        )

        async with ClaudeSDKClient(options=options) as client:
            await client.query(task)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            await channel.twitch(
                                target,
                                [MarkdownSegment(data=cast(MarkdownData, block))],
                            )


def _summarize(tool_name: str, tool_input: Any) -> str:
    if isinstance(tool_input, dict):
        if "command" in tool_input:
            return f"`{tool_input['command']}`"
        if "file_path" in tool_input:
            return f"File: {tool_input['file_path']}"
    return tool_name
