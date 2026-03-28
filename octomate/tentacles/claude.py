from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import TYPE_CHECKING, Any, cast

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
    rename_session,
)
from claude_agent_sdk.types import (
    HookContext,
    HookInput,
    Message,
    PermissionResult,
    PreToolUseHookInput,
    SyncHookJSONOutput,
    ToolPermissionContext,
)

from octomate.schemas.events import MessageEvent

try:
    from claude_agent_sdk import ThinkingBlock as _ThinkingBlock
except ImportError:
    _ThinkingBlock = None

from anthropic import AsyncAnthropic

from octomate.config import ClaudeCodeConfig
from octomate.schemas.session import SessionKey
from octomate.tentacles.base import (
    AgentTentacle,
    PlatformMessage,
    SendTarget,
    StreamSink,
)
from octomate.transmuters.interactions import Todo

if TYPE_CHECKING:
    from octomate.octopus import Octopus

logger = logging.getLogger(__name__)

AUTO_APPROVE = {"AskUserQuestion", "TodoWrite", "ExitPlanMode"}


class ClaudeCodeTentacle(AgentTentacle):
    config: ClaudeCodeConfig
    _vscode_uri: str  # precomputed vscode://file<cwd> launch link
    _worktrees_dir: str  # absolute path where per-session worktrees are created
    _todo_ts: dict[SessionKey, str]  # thread_key -> todo card message ts
    _session_id_map: dict[SessionKey, str]  # session_key -> Claude SDK session_id
    _session_names: dict[SessionKey, str]  # session_key -> human-readable task name
    _active_clients: dict[SessionKey, ClaudeSDKClient]

    def __init__(self, tag: str, octopus: Octopus, config: ClaudeCodeConfig) -> None:
        super().__init__(tag, octopus, description=config.description)
        self.config = config
        self._vscode_uri = f"vscode://file{os.path.abspath(config.cwd)}"
        self._worktrees_dir = (
            os.path.abspath(config.worktrees_dir)
            if config.worktrees_dir
            else os.path.join(os.path.abspath(config.cwd), ".octomate/worktrees")
        )
        self._todo_ts = {}
        self._session_id_map = {}
        self._session_names = {}
        self._active_clients = {}

    async def interrupt(self, key: SessionKey) -> None:
        client = self._active_clients.get(key)
        if client is not None:
            try:
                await client.interrupt()
            except Exception:
                logger.debug("ClaudeCodeTentacle: interrupt failed for [%s]", key)

    async def run(
        self,
        key: SessionKey,
        contents: list[MessageEvent],
    ):
        task = "".join([str(part) for part in contents[0].to_content_parts()])
        channel = self.octopus.tentacles[key.tentacle_id]
        target = SendTarget(
            chat_id=contents[0].chat_id,
            chat_type="group" if key.group_id else "private",
            reply_to=key.thread_id or None,
        )

        # Generate a human-readable name for this session from the task text,
        # or reuse the existing one for follow-up messages in the same thread.
        session_name = self._session_names.get(key)
        if not session_name:
            async with channel.open_stream(target) as stream:
                await stream.set_status("🤔 Generating session name…")
                try:
                    session_name = await asyncio.wait_for(
                        _generate_session_name(task), timeout=10.0
                    )
                except (asyncio.TimeoutError, Exception):
                    session_name = _make_session_name(task)
            self._session_names[key] = session_name

        todo_ts: str | None = self._todo_ts.get(key)

        async def hook_ask_user(
            input_data: HookInput, tool_use_id: str | None, context: HookContext
        ) -> SyncHookJSONOutput:
            tool_input = cast(PreToolUseHookInput, input_data)["tool_input"]
            questions = tool_input.get("questions", [])
            answers: dict[str, str] = {}
            for q in questions:
                text = q.get("question", "")
                options = [opt["label"] for opt in q.get("options", [])] or None
                multi = q.get("multiSelect", False)
                resp = await channel.feelers.questions.ask_question(
                    target, text, session_key=key, options=options, multi_select=multi
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
            nonlocal todo_ts
            tool_input = cast(PreToolUseHookInput, input_data)["tool_input"]
            todos = tool_input.get("todos", [])
            items: list[Todo] = []
            for item in todos:
                title = item.get("content", "")
                status = item.get("status", "pending")
                active_form = item.get("activeForm")
                mapped = {
                    "completed": "completed",
                    "in_progress": "in_progress",
                    "cancelled": "cancelled",
                }.get(status, "pending")
                items.append(
                    Todo(
                        todo_id="",
                        title=title,
                        status=mapped,
                        active_form=active_form,
                    )
                )
            new_ts = await channel.feelers.todos.upsert_todo_list(
                target, items, existing_ts=todo_ts
            )
            if new_ts:
                # Only (re-)pin when a new card was posted (ts changed).
                # bookmark_upsert is idempotent so no run-level guard is needed.
                if new_ts != todo_ts:
                    pinned = await channel.feelers.todos.pin_todo(target, new_ts)
                    if not pinned:
                        logger.warning(
                            "hook_todo_write: failed to pin todo card ts=%s", new_ts
                        )
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
            # Check session allowlist — skip confirmation if already allowed
            thread = await channel.threads.get(key)
            if channel.feelers.confirm.is_session_allowed(str(thread.id), tool_name):
                return PermissionResultAllow()
            action, future = await channel.feelers.confirm.create_confirmation(
                key=key,
                tool_name=tool_name,
                tool_call_id="",
                args=input_data,
                title=f"Claude Code: {tool_name}",
                description=_summarize(tool_name, input_data),
                skill="claude_code",
            )
            sent = await channel.feelers.confirm.send_confirmation(target, action)
            if not sent:
                await channel.feelers.confirm.expire_confirmation(
                    action.confirmation_id
                )
                return PermissionResultDeny(
                    message="Could not deliver approval request"
                )
            try:
                approved = await asyncio.wait_for(
                    future, timeout=channel.feelers.confirm.timeout
                )
            except TimeoutError:
                await channel.feelers.confirm.expire_confirmation(
                    action.confirmation_id
                )
                await channel.feelers.confirm.send_timeout_notification(target, action)
                return PermissionResultDeny(message="Timed out")
            except asyncio.CancelledError:
                await channel.feelers.confirm.expire_confirmation(
                    action.confirmation_id
                )
                await channel.feelers.confirm.send_timeout_notification(target, action)
                raise
            if approved:
                return PermissionResultAllow()
            return PermissionResultDeny(message="Denied by user")

        async def hook_exit_plan_mode(
            input_data: HookInput, tool_use_id: str | None, context: HookContext
        ) -> SyncHookJSONOutput:
            tool_input = cast(PreToolUseHookInput, input_data)["tool_input"]
            plan_notes = tool_input.get("plan") or tool_input.get("notes") or ""
            prompt = "📋 *Claude has finished planning and is ready to implement.*"
            if plan_notes:
                # Format plan with proper line breaks and code blocks for readability
                formatted_plan = plan_notes.replace("\\n", "\n")
                prompt += f"\n\n```\n{formatted_plan}\n```"
            prompt += "\n\n_Proceed, or type instructions to refine the plan:_"
            resp = await channel.feelers.questions.ask_question(
                target,
                prompt,
                session_key=key,
                options=["✅ Proceed with implementation"],
            )
            if resp is None or resp.answer == "✅ Proceed with implementation":
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                    }
                }
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"User wants to refine the plan. Instructions: {resp.answer}",
                }
            }

        session_id = self._session_id_map.get(key)
        if session_id is None:
            # Cold start or restart: try to recover the session from the DB so
            # we can resume the Claude Code session instead of starting fresh.
            session_id = await channel.threads.get_agent_session_id(key)
            if session_id:
                self._session_id_map[key] = session_id
                logger.debug(
                    "ClaudeCodeTentacle: recovered session_id %s for %s",
                    session_id,
                    key,
                )

        # Worktree setup — always create an isolated worktree per session.
        # ThreadStore.get_worktree_info already handles the LRU cache, so no
        # separate in-memory map is needed here.
        worktree_path: str | None = None
        branch_name: str | None = None

        info = await channel.threads.get_worktree_info(key)
        if info:
            worktree_path, branch_name = info
        else:
            thread_identifier = key.thread_id or (
                contents[0].message_id if contents else ""
            )
            branch_name = hashlib.sha1(thread_identifier.encode()).hexdigest()[:8]
            worktree_path = await _create_worktree(
                base_dir=os.path.abspath(self.config.cwd),
                worktrees_dir=self._worktrees_dir,
                branch_name=branch_name,
            )
            await channel.threads.set_worktree_info(key, worktree_path, branch_name)

        options = ClaudeAgentOptions(
            cwd=worktree_path if worktree_path else self.config.cwd,
            model=self.config.model or None,
            permission_mode="acceptEdits",
            max_turns=self.config.max_turns,
            can_use_tool=can_use_tool,
            resume=session_id,
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher="AskUserQuestion", hooks=[hook_ask_user]),
                    HookMatcher(matcher="TodoWrite", hooks=[hook_todo_write]),
                    HookMatcher(matcher="ExitPlanMode", hooks=[hook_exit_plan_mode]),
                ],
            },
        )

        result_text: str = ""
        has_text = False
        try:
            async with channel.open_stream(target) as stream:
                await stream.set_status(f"🐙 *{session_name}* — starting…")
                async with ClaudeSDKClient(options=options) as client:
                    self._active_clients[key] = client
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
        finally:
            self._active_clients.pop(key, None)
            if todo_ts:
                await channel.feelers.todos.unpin_todo(target, todo_ts)
                self._todo_ts.pop(key, None)

        session_id_after = self._session_id_map.get(key)

        # Persist the session_id so it survives process restarts.
        if session_id_after:
            await channel.threads.set_agent_session_id(key, session_id_after)

        # Apply the session name to the Claude Code session so it shows up
        # in the Claude Code UI / session list.
        if session_id_after:
            try:
                rename_session(session_id_after, session_name)
            except Exception:
                logger.debug("Failed to rename Claude Code session", exc_info=True)

        resume_uri = (
            f"vscode://anthropic.claude-code/open?session={session_id_after}"
            if session_id_after
            else None
        )
        if worktree_path and branch_name:
            # Worktree mode: link points to the isolated branch folder
            vscode_link_uri = f"vscode://file{worktree_path}"
            footer_text = (
                f"🌿 *{branch_name}* · <{vscode_link_uri}|Open branch in VSCode>"
            )
            plain_content = f"Open branch in VSCode: {worktree_path}"
        else:
            # Legacy mode (no worktrees_dir configured): keep original behavior
            vscode_link_uri = self._vscode_uri
            footer_text = (
                f"🖥️ *{session_name}* · <{vscode_link_uri}|Open project in VSCode>"
            )
            plain_content = f"Open project in VSCode: {vscode_link_uri}"
        if resume_uri:
            footer_text += f"  ·  🔄 <{resume_uri}|Resume in VS Code Claude>"
        await channel.send_platform_message(
            chat_id=str(target.chat_id),
            chat_type=target.chat_type,
            reply_to=str(target.reply_to),
            messages=[
                PlatformMessage(
                    msg_type="text",
                    content=plain_content,
                    metadata={
                        "blocks": [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": footer_text,
                                },
                            }
                        ]
                    },
                )
            ],
        )

        # When this was a root (non-threaded) message, the session state was
        # stored under a key with thread_id=None.  Follow-up messages in the
        # Slack thread that Claude just replied into will arrive with
        # thread_id == contents[0].message_id (the root message ts).  Pre-
        # register all per-session state under that thread-continuation key so
        # subsequent calls can look it up and resume the same Claude session.
        if not key.thread_id and contents and contents[0].message_id:
            thread_key = key._replace(thread_id=contents[0].message_id)
            if sid := self._session_id_map.get(key):
                self._session_id_map[thread_key] = sid
                # Persist under the thread-continuation key too so follow-ups
                # can resume the session even after a restart.
                try:
                    await channel.threads.set_agent_session_id(thread_key, sid)
                except Exception:
                    logger.warning(
                        "ClaudeCodeTentacle: failed to persist session_id for thread_key",
                        exc_info=True,
                    )
            if ts := self._todo_ts.get(key):
                self._todo_ts[thread_key] = ts
            if name := self._session_names.get(key):
                self._session_names[thread_key] = name
            if wt := await channel.threads.get_worktree_info(key):
                await channel.threads.set_worktree_info(thread_key, wt[0], wt[1])


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
        summary = _summarize(block.name, block.input)
        status = f"🔧 {block.name}" + (f" {summary}" if summary else "")
        await stream.set_status(status)
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


async def _generate_session_name(task: str) -> str:
    """Generate a concise 3-5 word session name using Claude Haiku.

    Falls back to first-line extraction on timeout or any API error so the
    session always gets a name even if the naming call fails.
    """
    try:
        async with AsyncAnthropic() as client:
            msg = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5",
                    max_tokens=20,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "Write a concise 3-5 word title for this task. "
                                "Reply with ONLY the title, no quotes or punctuation:\n\n"
                                f"{task[:500]}"
                            ),
                        }
                    ],
                ),
                timeout=3.0,
            )
        text = getattr(msg.content[0], "text", None) if msg.content else None
        if text:
            text = text.strip()
            return (text[:47] + "…") if len(text) > 50 else text
    except Exception:
        logger.debug("Session name generation failed, using fallback", exc_info=True)
    return _make_session_name(task)


def _make_session_name(task: str) -> str:
    """Derive a short human-readable session name from the task description."""
    for line in task.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return (line[:47] + "…") if len(line) > 50 else line
    return "Session"


def _summarize(tool_name: str, tool_input: Any) -> str:
    if isinstance(tool_input, dict):
        if "command" in tool_input:
            return f"`{tool_input['command']}`"
        if "file_path" in tool_input:
            return f"`{tool_input['file_path']}`"
        if "pattern" in tool_input:
            detail = f"`{tool_input['pattern']}`"
            if "path" in tool_input:
                detail += f" in `{tool_input['path']}`"
            elif "glob" in tool_input:
                detail += f" ({tool_input['glob']})"
            return detail
    return ""


async def _create_worktree(base_dir: str, worktrees_dir: str, branch_name: str) -> str:
    """Create a git worktree for branch_name under worktrees_dir.

    Returns the absolute path of the newly created worktree directory.
    Raises RuntimeError if git exits non-zero.
    """
    # Worktree directory name: replace "/" in branch_name with "-"
    worktree_path = os.path.join(worktrees_dir, branch_name.replace("/", "-"))
    os.makedirs(worktrees_dir, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "git",
        "worktree",
        "add",
        worktree_path,
        "-b",
        branch_name,
        cwd=base_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"git worktree add failed (branch={branch_name}): {stderr.decode().strip()}"
        )
    return os.path.abspath(worktree_path)


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
