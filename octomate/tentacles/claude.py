from __future__ import annotations

import asyncio
import logging
import uuid
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

from octomate.schemas.events import MessageEvent

try:
    from claude_agent_sdk import ThinkingBlock as _ThinkingBlock
except ImportError:
    _ThinkingBlock = None

from octomate.config import ClaudeCodeConfig
from octomate.schemas.session import SessionKey
from octomate.store import TodoItem
from octomate.tentacles.base import (
    AgentTentacle,
    SendTarget,
    StreamSink,
)

if TYPE_CHECKING:
    from octomate.octopus import Octopus

logger = logging.getLogger(__name__)

AUTO_APPROVE = {"AskUserQuestion", "TodoWrite"}


class ClaudeCodeTentacle(AgentTentacle):
    config: ClaudeCodeConfig
    _todo_ts: dict[SessionKey, str]  # thread_key -> pinned message ts
    _todo_id_maps: dict[
        SessionKey, dict[str, str]
    ]  # thread_key -> title -> stable uuid
    _session_id_map: dict[SessionKey, str]  # session_key -> Claude SDK session_id

    def __init__(self, tag: str, octopus: Octopus, config: ClaudeCodeConfig) -> None:
        super().__init__(tag, octopus, description=config.description)
        self.config = config
        self._todo_ts = {}
        self._todo_id_maps = {}
        self._session_id_map = {}

    async def __call__(
        self,
        key: SessionKey,
        contents: list[MessageEvent],  # should always length 1 for agent tentacles
    ):
        task = "".join([str(part) for part in contents[0].to_content_parts()])
        channel = self.octopus.tentacles["slack"]
        target = SendTarget(
            chat_id=contents[0].chat_id,
            chat_type="group" if key.group_id else "private",
            reply_to=key.thread_id or None,
        )

        todo_id_map: dict[str, str] = self._todo_id_maps.setdefault(key, {})
        todo_ts: str | None = self._todo_ts.get(key)
        _todo_pinned_this_run = False

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
            nonlocal todo_ts, _todo_pinned_this_run
            tool_input = input_data.get("tool_input", {})
            todos = tool_input.get("todos", [])
            items: list[TodoItem] = []
            for item in todos:
                title = item.get("content", "")
                status = item.get("status", "pending")
                active_form = item.get("activeForm")
                if title not in todo_id_map:
                    todo_id_map[title] = uuid.uuid4().hex
                mapped = {
                    "completed": "completed",
                    "in_progress": "in_progress",
                    "cancelled": "cancelled",
                }.get(status, "pending")
                items.append(
                    TodoItem(
                        todo_id=todo_id_map[title],
                        title=title,
                        status=mapped,
                        active_form=active_form,
                    )
                )
            new_ts = await channel.feelers.todos.upsert_todo_list(
                target, items, existing_ts=todo_ts
            )
            if new_ts:
                # Pin on first call of each run (re-establishes pin after restart)
                # or whenever the message ts changes (new card was created).
                if new_ts != todo_ts or not _todo_pinned_this_run:
                    await channel.feelers.todos.pin_todo(target, new_ts)
                    _todo_pinned_this_run = True
                todo_ts = new_ts
                self._todo_ts[key] = new_ts
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
            if tool_name in AUTO_APPROVE:
                return PermissionResultAllow()
            action, future = self.octopus.store.create_confirmation(
                session_key=key,
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

        session_id = self._session_id_map.get(key)
        options = ClaudeAgentOptions(
            cwd=self.config.cwd,
            model=self.config.model or None,
            permission_mode="acceptEdits",
            max_turns=self.config.max_turns,
            can_use_tool=can_use_tool,
            resume=session_id,
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
                        if msg.session_id:
                            self._session_id_map[key] = msg.session_id
                        continue
                    has_text = await _handle_stream_msg(msg, stream, has_text)

        # When this was a root (non-threaded) message, the session state was
        # stored under a key with thread_id=None.  Follow-up messages in the
        # Slack thread that Claude just replied into will arrive with
        # thread_id == contents[0].message_id (the root message ts).  Pre-
        # register all per-session state under that thread-continuation key so
        # subsequent calls can look it up and resume the same Claude session.
        if key.thread_id is None and contents and contents[0].message_id:
            thread_key = key._replace(thread_id=contents[0].message_id)
            if sid := self._session_id_map.get(key):
                self._session_id_map[thread_key] = sid
            if ts := self._todo_ts.get(key):
                self._todo_ts[thread_key] = ts
            if key in self._todo_id_maps:
                self._todo_id_maps[thread_key] = self._todo_id_maps[key]


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
            await stream.post_thinking_block(text)
            # post_thinking_block() calls flush() internally, so the text stream
            # is reset; the next TextBlock must open a fresh Slack message.
            return False
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
