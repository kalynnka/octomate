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

from octomate.config import ClaudeCodeConfig
from octomate.schemas.segments import AgentSegment, MarkdownSegment, TextSegment
from octomate.schemas.session import SessionKey
from octomate.tentacles.base import AgentTentacle, ChannelTentacle, SendTarget

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

        async def can_use_tool(
            tool_name: str,
            input_data: dict[str, Any],
            ctx: ToolPermissionContext,
        ) -> PermissionResult:
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
        async with ClaudeSDKClient(options=options) as client:
            await client.query(task)
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    result_text += msg.result or ""
                segments = _segments_from(msg)
                if segments:
                    await channel.twitch(target, segments)

        return result_text


def _segments_from(msg: Message) -> list[AgentSegment]:
    match msg:
        case AssistantMessage(error=err) if err:
            return [TextSegment(data={"text": f"⚠ Error: {err}"})]
        case AssistantMessage(content=blocks):
            return [seg for b in blocks if (seg := _segment_from_block(b))]
        case SystemMessage(subtype="task_started"):
            return [TextSegment(data={"text": "🚀 Task started"})]
        case SystemMessage(subtype="task_progress", data=data) if data.get(
            "last_tool_name"
        ):
            return [
                TextSegment(data={"text": f"⏳ Working… ({data['last_tool_name']})"})
            ]
        case SystemMessage(subtype="task_notification", data=data) if data.get(
            "summary"
        ):
            return [
                MarkdownSegment(
                    data={"text": f"📋 {data.get('status', '')}: {data['summary']}"}
                )
            ]
        case RateLimitEvent() if msg.rate_limit_info.status != "allowed":
            return [TextSegment(data={"text": "⏳ Rate limited, waiting…"})]
    return []


def _segment_from_block(block: ContentBlock) -> AgentSegment | None:
    match block:
        case TextBlock(text=text):
            return MarkdownSegment(data={"text": text})
        case ToolUseBlock(name=name, input=inp):
            return MarkdownSegment(data={"text": f"`{name}` {_summarize(name, inp)}"})
        case ToolResultBlock(is_error=True, content=content):
            return MarkdownSegment(
                data={"text": f"❌ Error: {str(content or 'unknown error')[:200]}"}
            )
        case ToolResultBlock(content=content) if content:
            text = str(content)
            return MarkdownSegment(
                data={"text": text[:300] + "…" if len(text) > 300 else text}
            )
    return None


def _summarize(tool_name: str, tool_input: Any) -> str:
    if isinstance(tool_input, dict):
        if "command" in tool_input:
            return f"`{tool_input['command']}`"
        if "file_path" in tool_input:
            return f"File: {tool_input['file_path']}"
    return tool_name
