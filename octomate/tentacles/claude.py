from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ContentBlock,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import (
    HookContext,
    HookInput,
    Message,
    PermissionResult,
    SyncHookJSONOutput,
    ToolPermissionContext,
)

try:
    from claude_agent_sdk import ThinkingBlock as _ThinkingBlock
except ImportError:
    _ThinkingBlock = None

from octomate.config import ClaudeCodeConfig
from octomate.schemas.session import SessionKey
from octomate.tentacles.base import (
    AgentTentacle,
    ChannelTentacle,
    SendTarget,
    StreamSink,
)

if TYPE_CHECKING:
    from octomate.octopus import Octopus

logger = logging.getLogger(__name__)


class ClaudeCodeTentacle(AgentTentacle):
    config: ClaudeCodeConfig

    def __init__(self, tag: str, octopus: Octopus, config: ClaudeCodeConfig) -> None:
        super().__init__(tag, octopus, description=config.description)
        self.config = config

    async def __call__(
        self, task: str, channel: ChannelTentacle, target: SendTarget
    ) -> str:
        session_key = SessionKey(
            tentacle_id=channel.tag,
            user_id=str(target.chat_id),
        )
        todo_map: dict[str, str] = {}

        async def hook_ask_user(
            input_data: HookInput, tool_use_id: str | None, context: HookContext
        ) -> SyncHookJSONOutput:
            tool_input = input_data.get("tool_input", {})
            questions = tool_input.get("questions", [])
            answers: dict[str, str] = {}
            for q in questions:
                text = q.get("question", "")
                options = [opt["label"] for opt in q.get("options", [])] or None
                multi = q.get("multiSelect", False)
                if multi:
                    text += "\n(select multiple, comma-separated)"
                resp = await channel.feelers.questions.ask_question(
                    target, text, options, multi_select=multi
                )
                if resp:
                    answers[q["question"]] = resp.answer
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": {"questions": questions, "answers": answers},
                }
            }

        async def hook_todo_write(
            input_data: HookInput, tool_use_id: str | None, context: HookContext
        ) -> SyncHookJSONOutput:
            tool_input = input_data.get("tool_input", {})
            todos = tool_input.get("todos", [])
            for item in todos:
                title = item.get("content", "")
                status = item.get("status", "pending")
                active_form = item.get("activeForm")
                existing_id = todo_map.get(title)
                if existing_id is None:
                    todo = await channel.feelers.todos.create_todo(
                        target, title, active_form=active_form
                    )
                    if todo:
                        todo_map[title] = todo.todo_id
                        if status != "pending":
                            mapped = (
                                "completed"
                                if status == "completed"
                                else "in_progress"
                                if status == "in_progress"
                                else "pending"
                            )
                            await channel.feelers.todos.update_todo(
                                todo.todo_id, mapped
                            )
                else:
                    mapped = (
                        "completed"
                        if status == "completed"
                        else "in_progress"
                        if status == "in_progress"
                        else "pending"
                    )
                    await channel.feelers.todos.update_todo(existing_id, mapped)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                }
            }

        _AUTO_APPROVE = {"AskUserQuestion", "TodoWrite"}

        async def can_use_tool(
            tool_name: str,
            input_data: dict[str, Any],
            ctx: ToolPermissionContext,
        ) -> PermissionResult:
            if tool_name in _AUTO_APPROVE:
                return PermissionResultAllow()
            action, future = self.octopus.store.create_confirmation(
                session_key=session_key,
                tool_name=tool_name,
                tool_call_id="",
                args=input_data,
                title=f"Claude Code: {tool_name}",
                description=_summarize(tool_name, input_data),
                skill="claude_code",
            )
            sent = await channel.send_confirmation(target, action)
            if not sent:
                self.octopus.store.expire_confirmation(action.confirmation_id)
                return PermissionResultDeny(
                    message="Could not deliver approval request"
                )
            try:
                approved = await asyncio.wait_for(
                    future, timeout=self.octopus.store.timeout
                )
            except TimeoutError:
                self.octopus.store.expire_confirmation(action.confirmation_id)
                return PermissionResultDeny(message="Timed out")
            if approved:
                return PermissionResultAllow()
            return PermissionResultDeny(message="Denied by user")

        options = ClaudeAgentOptions(
            cwd=self.config.cwd,
            model=self.config.model or None,
            max_turns=self.config.max_turns,
            can_use_tool=can_use_tool,
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher="AskUserQuestion", hooks=[hook_ask_user]),
                    HookMatcher(matcher="TodoWrite", hooks=[hook_todo_write]),
                ],
            },
        )

        result_text: str = ""
        has_text = False
        async with channel.open_stream(target) as stream:
            await stream.set_status("🚀 Starting…")
            async with ClaudeSDKClient(options=options) as client:
                await client.query(task)
                async for msg in client.receive_response():
                    logger.debug(
                        "ClaudeCodeTentacle recv %s: %s",
                        type(msg).__name__,
                        _msg_sample(msg),
                    )
                    if isinstance(msg, ResultMessage):
                        result_text += msg.result or ""
                        continue
                    has_text = await _handle_stream_msg(msg, stream, has_text)
            # Send the final result text (ResultMessage) to the channel.
            # This is the authoritative summary produced by Claude Code at the
            # end of a run and must be forwarded regardless of what streaming
            # text blocks were already sent above.
            if result_text:
                if has_text:
                    await stream.append("\n\n")
                await stream.append(result_text)

        return result_text


async def _handle_stream_msg(msg: Message, stream: StreamSink, has_text: bool) -> bool:
    if isinstance(msg, AssistantMessage):
        if msg.error:
            if has_text:
                await stream.append("\n\n")
            await stream.append(f"⚠ Error: {msg.error}")
            return True
        for block in msg.content:
            has_text = await _handle_stream_block(block, stream, has_text)
    elif isinstance(msg, SystemMessage):
        if msg.subtype == "task_started":
            await stream.set_status("🚀 Task started")
        elif msg.subtype == "task_progress" and msg.data.get("last_tool_name"):
            await stream.set_status(f"⏳ Working… ({msg.data['last_tool_name']})")
        elif msg.subtype == "task_notification" and msg.data.get("summary"):
            await stream.set_status(
                f"📋 {msg.data.get('status', '')}: {msg.data['summary']}"
            )
    elif isinstance(msg, RateLimitEvent) and msg.rate_limit_info.status != "allowed":
        await stream.set_status("⏳ Rate limited, waiting…")
    return has_text


async def _handle_stream_block(
    block: ContentBlock, stream: StreamSink, has_text: bool
) -> bool:
    if isinstance(block, TextBlock):
        if has_text:
            await stream.append("\n\n")
        await stream.append(block.text)
        return True
    if isinstance(block, ToolUseBlock):
        # Flush any buffered pre-tool text so it lands in its own message block
        # *before* the confirmation card.  After the flush, message_ts is reset
        # to None, so the next TextBlock will open a fresh message that appears
        # chronologically after the card in the thread.
        await stream.flush()
        await stream.set_status(
            f"🔧 {block.name} {_summarize(block.name, block.input)}"
        )
        return False  # new block — no prior text in this segment yet
    elif isinstance(block, ToolResultBlock) and block.is_error:
        if has_text:
            await stream.append("\n\n")
        await stream.append(f"❌ Error: {str(block.content or 'unknown error')[:200]}")
        return True
    elif _ThinkingBlock and isinstance(block, _ThinkingBlock):
        text = getattr(block, "thinking", "") or ""
        await stream.set_status("🧠 Thinking…")  # always surface the indicator
        if text:
            await stream.append(text)  # send full thinking content to the channel
            return True
    return has_text


def _summarize(tool_name: str, tool_input: Any) -> str:
    if isinstance(tool_input, dict):
        if "command" in tool_input:
            return f"`{tool_input['command']}`"
        if "file_path" in tool_input:
            return f"File: {tool_input['file_path']}"
    return tool_name


def _msg_sample(msg: Message) -> str:
    if isinstance(msg, ResultMessage):
        return (msg.result or "")[:120]
    if isinstance(msg, AssistantMessage):
        if msg.error:
            return f"error={msg.error[:120]}"
        parts = []
        for b in msg.content[:3]:
            if isinstance(b, TextBlock):
                parts.append(f"Text({b.text[:80]})")
            elif isinstance(b, ToolUseBlock):
                parts.append(f"ToolUse({b.name})")
            elif isinstance(b, ToolResultBlock):
                parts.append(f"ToolResult(err={b.is_error})")
            else:
                parts.append(type(b).__name__)
        return ", ".join(parts)
    if isinstance(msg, SystemMessage):
        return f"subtype={msg.subtype} data={str(msg.data)[:100]}"
    if isinstance(msg, RateLimitEvent):
        return f"status={msg.rate_limit_info.status}"
    return str(msg)[:120]
