"""Route a driven SDK stream's child messages to the Agent call that owns them."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    StreamEvent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from uuid_utils.compat import uuid7

from octomate.events import (
    SubagentActivityStatus,
    SubagentSettledEvent,
    SubagentStartedEvent,
)
from octomate.managers.conversation import ConversationManager
from octomate.schemas.conversation import Conversation
from octomate.tentacles.claude.adapter import ClaudeRunAccumulator, subagent_name


@dataclass
class DrivenSubagents:
    """A driven run's subagents, each kept as a run of its own child conversation."""

    conversations: ConversationManager
    parent: Conversation
    cwd: Path
    # Open children, by the Agent call that spawned each.
    calls: dict[str, tuple[ClaudeRunAccumulator, str | None]] = field(
        default_factory=dict
    )
    # Agent ids that started and have not settled.
    working: list[str] = field(default_factory=list)

    async def watch[T](
        self, stream: AsyncIterable[T]
    ) -> AsyncGenerator[T | SubagentSettledEvent]:
        """`stream` without the messages private to a child, and with a settle for
        every subagent it started. Claude fires no `SubagentStop` for one it
        interrupts, so one left open settles with the run: cancelled, or failed."""
        try:
            async for item in stream:
                if isinstance(item, SubagentStartedEvent):
                    self.working.append(item.invocation_id)
                elif isinstance(item, SubagentSettledEvent):
                    if item.invocation_id not in self.working:
                        continue
                    self.working.remove(item.invocation_id)
                elif isinstance(
                    item, AssistantMessage | UserMessage | StreamEvent
                ) and await self.receive(item):
                    continue
                yield item
        except (Exception, asyncio.CancelledError) as error:
            cancelled = isinstance(error, asyncio.CancelledError)
            for settled in self.abandon("cancelled" if cancelled else "failed"):
                yield settled
            raise
        for settled in self.abandon("cancelled"):
            yield settled

    def abandon(self, status: SubagentActivityStatus) -> list[SubagentSettledEvent]:
        settled = [
            SubagentSettledEvent(invocation_id=agent_id, status=status)
            for agent_id in self.working
        ]
        self.working.clear()
        return settled

    async def receive(
        self, message: AssistantMessage | UserMessage | StreamEvent
    ) -> bool:
        """True for a message private to a child. Its spawn and return stay on the parent."""
        if message.parent_tool_use_id:
            if child := self.calls.get(message.parent_tool_use_id):
                accumulator, _name = child
                for _ in accumulator.consume(message):
                    pass
            return True
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock) and block.name in {
                    "Agent",
                    "Task",
                    "Skill",
                }:
                    prompt = block.input.get("prompt")
                    child = ClaudeRunAccumulator()
                    child.begin(prompt if isinstance(prompt, str) else None)
                    self.calls[block.id] = child, subagent_name(block)
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            # The Agent call's result names the child Claude ran for it. A refused
            # call's is plain text, whatever the SDK's type says.
            receipt = message.tool_use_result
            agent_id = receipt.get("agentId") if isinstance(receipt, dict) else None
            for block in message.content:
                if (
                    isinstance(block, ToolResultBlock)
                    and block.tool_use_id in self.calls
                ):
                    await self.record(block.tool_use_id, agent_id)
        return False

    async def record(self, call_id: str, agent_id: str | None) -> None:
        child, name = self.calls.pop(call_id)
        if agent_id is None:
            return  # A refused Agent call ran no child.
        conversation = await self.conversations.ensure(
            self.parent.thread_id,
            agent_tentacle_id=self.parent.agent_tentacle_id,
            subagent_id=agent_id,
            name=name,
            parent_conversation_id=self.parent.id,
            with_runs=False,
        )
        # Saving the parent run links this one to it, by the call it answers.
        await self.conversations.record_agent_run(
            conversation,
            run_id=str(uuid7()),
            messages=child.messages,
            name=self.parent.agent_tentacle_id,
            model_name=child.model_name,
            context_used=child.context_used,
            cwd=self.cwd,
            external_id=agent_id,
            parent_tool_call_id=call_id,
        )
