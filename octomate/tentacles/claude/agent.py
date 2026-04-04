from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
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
from uuid_utils import uuid7

from octomate.schemas.events import MessageEvent

try:
    from claude_agent_sdk import ThinkingBlock as _ThinkingBlock
except ImportError:
    _ThinkingBlock = None

from octomate.config import ClaudeCodeConfig
from octomate.nerve import (
    AskUser,
    ConfirmResult,
    ConfirmTool,
    DismissPending,
    NerveStream,
    SendSegments,
    TodoResult,
    TodoUpdate,
    UserAnswer,
)
from octomate.schemas.segments import MarkdownSegment
from octomate.schemas.session import SessionKey
from octomate.stores.thread import ThreadStore
from octomate.tentacles.base import AgentTentacle
from octomate.transmuters.interactions import Todo

if TYPE_CHECKING:
    from octomate.octopus import Octopus

logger = logging.getLogger(__name__)

AUTO_APPROVE = {"AskUserQuestion", "TodoWrite", "ExitPlanMode"}
TODO_UPDATE_TIMEOUT = 30.0  # seconds to wait for TodoResult from octopus handler


class ClaudeCodeTentacle(AgentTentacle):
    config: ClaudeCodeConfig
    threads: ThreadStore
    _vscode_uri: str  # precomputed vscode://file<cwd> launch link
    _todo_card_refs: dict[SessionKey, str]  # session_key -> active todo card reference
    _session_id_map: dict[SessionKey, str]  # session_key -> Claude SDK session_id
    _active_clients: dict[SessionKey, ClaudeSDKClient]
    _session_names: dict[SessionKey, str]  # session_key -> AI-generated session name
    _worktree_paths: dict[SessionKey, str]  # session_key -> current worktree path

    def __init__(self, tag: str, octopus: Octopus, config: ClaudeCodeConfig) -> None:
        super().__init__(tag, octopus, description=config.description, handover=True)
        self.config = config
        self.threads = ThreadStore(default_owner=tag)
        if config.ssh:
            self._vscode_uri = (
                f"vscode://vscode-remote/ssh-remote+{config.ssh.host}{config.cwd}"
            )
        else:
            self._vscode_uri = f"vscode://file{os.path.abspath(config.cwd)}"
        self._todo_card_refs = {}
        self._session_id_map = {}
        self._active_clients = {}
        self._session_names = {}
        self._worktree_paths = {}

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
        *,
        session_name: str = "",
    ):
        task = "".join([str(part) for part in contents[0].to_content_parts()])

        nerve = self.octopus.agent_nerve

        async def hook_ask_user(
            input_data: HookInput, tool_use_id: str | None, context: HookContext
        ) -> SyncHookJSONOutput:
            tool_input = cast(PreToolUseHookInput, input_data)["tool_input"]
            questions = tool_input.get("questions", [])
            answers: dict[str, str] = {}
            for q in questions:
                text = q.get("question", "")
                options = [opt["label"] for opt in q.get("options", [])]
                multi = q.get("multiSelect", False)
                request_id = str(uuid7())
                future = self.octopus.pending.answers.create(request_id)
                await nerve.send(
                    AskUser(
                        key=key,
                        request_id=request_id,
                        question=text,
                        options=options,
                        multi_select=multi,
                    )
                )
                resp: UserAnswer = await future
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
            future = self.octopus.pending.todos.create(str(key))
            await nerve.send(
                TodoUpdate(
                    key=key,
                    items=items,
                    existing_card_ref=self._todo_card_refs.get(key),
                )
            )
            try:
                result: TodoResult = await asyncio.wait_for(
                    future, timeout=TODO_UPDATE_TIMEOUT
                )
                if result.card_ref:
                    self._todo_card_refs[key] = result.card_ref
            except TimeoutError:
                logger.warning("hook_todo_write: timed out waiting for TodoResult")
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
            request_id = str(uuid7())
            future = self.octopus.pending.confirmations.create(request_id)
            await nerve.send(
                ConfirmTool(
                    key=key,
                    request_id=request_id,
                    tool_name=tool_name,
                    args=input_data,
                    title=f"Claude Code: {tool_name}",
                    description=_summarize(tool_name, input_data),
                    skill="claude_code",
                )
            )
            try:
                result: ConfirmResult = await future
            except asyncio.CancelledError:
                raise
            if result.approved:
                return PermissionResultAllow()
            return PermissionResultDeny(message="Denied by user")

        async def hook_exit_plan_mode(
            input_data: HookInput, tool_use_id: str | None, context: HookContext
        ) -> SyncHookJSONOutput:
            tool_input = cast(PreToolUseHookInput, input_data)["tool_input"]
            plan_notes = tool_input.get("plan") or tool_input.get("notes") or ""
            prompt = "📋 *Claude has finished planning and is ready to implement.*"
            if plan_notes:
                formatted_plan = plan_notes.replace("\\n", "\n")
                prompt += f"\n\n```\n{formatted_plan}\n```"
            prompt += "\n\n_Proceed, or type instructions to refine the plan:_"
            request_id = str(uuid7())
            future = self.octopus.pending.answers.create(request_id)
            await nerve.send(
                AskUser(
                    key=key,
                    request_id=request_id,
                    question=prompt,
                    options=["✅ Proceed with implementation"],
                )
            )
            resp: UserAnswer = await future
            answer = resp.answer
            if answer in ("(no response)", "✅ Proceed with implementation"):
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
                    "permissionDecisionReason": f"User wants to refine the plan. Instructions: {answer}",
                }
            }

        session_id = self._session_id_map.get(key)
        if session_id is None:
            # Cold start or restart: try to recover the session from the DB so
            # we can resume the Claude Code session instead of starting fresh.
            session_id = await self.threads.get_agent_session_id(key)
            if session_id:
                self._session_id_map[key] = session_id
                logger.debug(
                    "ClaudeCodeTentacle: recovered session_id %s for %s",
                    session_id,
                    key,
                )

        # Generate session name upfront for new sessions
        if not session_id and not self._session_names.get(key):
            if session_name:
                self._session_names[key] = session_name
            else:
                self._session_names[key] = (
                    f"Session {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                )

        extra_args = {}
        if name := self._session_names.get(key):
            extra_args["name"] = name

        options = ClaudeAgentOptions(
            cwd=self.config.cwd,
            model=self.config.model or None,
            permission_mode="acceptEdits",
            max_turns=self.config.max_turns,
            can_use_tool=can_use_tool,
            resume=session_id,
            extra_args=extra_args,
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher="AskUserQuestion", hooks=[hook_ask_user]),
                    HookMatcher(matcher="TodoWrite", hooks=[hook_todo_write]),
                    HookMatcher(matcher="ExitPlanMode", hooks=[hook_exit_plan_mode]),
                ],
            },
        )

        transport = None
        if self.config.ssh:
            from octomate.tentacles.claude.transport import SshTransport

            transport = SshTransport(
                host=self.config.ssh.host,
                cwd=self.config.cwd,
                identity_file=self.config.ssh.identity_file,
                ssh_options=self.config.ssh.ssh_options,
                claude_bin=self.config.ssh.claude_bin,
            )

        result_text: str = ""
        has_text = False
        is_first_response = session_id is None
        try:
            async with NerveStream(nerve, key) as stream:
                await stream.set_status("🐙 Channeling")
                async with ClaudeSDKClient(
                    options=options, transport=transport
                ) as client:
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

                        # Detect worktree creation
                        worktree_path = _extract_worktree_path(msg)
                        if worktree_path:
                            self._worktree_paths[key] = worktree_path
                            await self.threads.set_worktree_info(key, worktree_path, "")

                        has_text = await _handle_stream_msg(msg, stream, has_text)
        finally:
            self._active_clients.pop(key, None)

        session_id_after = self._session_id_map.get(key)

        # Persist the session_id so it survives process restarts.
        if session_id_after:
            await self.threads.set_agent_session_id(key, session_id_after)

        # Persist session name for new sessions
        if is_first_response and session_id_after:
            stored_name = self._session_names.get(key)
            if stored_name:
                await self.threads.set_session_name(key, stored_name)

        # Use worktree path if available, otherwise use default cwd
        current_worktree = self._worktree_paths.get(key)
        if not current_worktree:
            worktree_info = await self.threads.get_worktree_info(key)
            if worktree_info:
                current_worktree = worktree_info[0]
                self._worktree_paths[key] = current_worktree

        if current_worktree:
            if self.config.ssh:
                vscode_link_uri = (
                    f"vscode://vscode-remote/ssh-remote+{self.config.ssh.host}"
                    f"{current_worktree}"
                )
            else:
                vscode_link_uri = f"vscode://file{os.path.abspath(current_worktree)}"
        else:
            vscode_link_uri = self._vscode_uri

        if session_id_after:
            resume_uri = (
                f"vscode://anthropic.claude-code/open?session={session_id_after}"
            )
            if self.config.ssh:
                resume_uri += f"&remote=ssh-remote+{self.config.ssh.host}"
        else:
            resume_uri = None

        # Get session name for display
        display_name = self._session_names.get(key)
        if not display_name:
            display_name = await self.threads.get_session_name(key)
            if display_name:
                self._session_names[key] = display_name

        footer_text = f"🖥️ <{vscode_link_uri}|Open project in VSCode>"
        if resume_uri:
            resume_text = "Resume in Claude Code"
            if display_name:
                resume_text = f"Resume: {display_name}"
            footer_text += f"  ·  🔄 <{resume_uri}|{resume_text}>"
        await nerve.send(
            SendSegments(
                key=key,
                segments=[MarkdownSegment(data={"text": footer_text})],
            )
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
                    await self.threads.set_agent_session_id(thread_key, sid)
                except Exception:
                    logger.warning(
                        "ClaudeCodeTentacle: failed to persist session_id for thread_key",
                        exc_info=True,
                    )
            if card_ref := self._todo_card_refs.get(key):
                self._todo_card_refs[thread_key] = card_ref

    async def _dismiss_pending(self, key: SessionKey) -> None:
        """Background task: send DismissPending with the current todo card ref."""
        try:
            card_ref = self._todo_card_refs.pop(key, None)
            await self.octopus.agent_nerve.send(
                DismissPending(key=key, card_ref=card_ref)
            )
        except Exception:
            logger.exception(
                "ClaudeCodeTentacle %s: error sending DismissPending for [%s]",
                self.id,
                key,
            )


async def _handle_stream_msg(msg: Message, stream: NerveStream, has_text: bool) -> bool:
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
    block: ContentBlock, stream: NerveStream, has_text: bool
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


def _extract_worktree_path(msg: Message) -> str | None:
    """Extract worktree path from tool results if present."""
    if not isinstance(msg, AssistantMessage):
        return None

    for block in msg.content:
        if isinstance(block, ToolResultBlock) and not block.is_error:
            content = str(block.content or "")
            # Look for worktree path patterns
            if "worktree" in content.lower() and (
                "path" in content.lower() or "/" in content
            ):
                # Extract path-like strings
                import re

                matches = re.findall(
                    r"(?:path|worktree)[:\s]+([/\w\-\.]+)", content, re.IGNORECASE
                )
                if matches:
                    return matches[0]
    return None
