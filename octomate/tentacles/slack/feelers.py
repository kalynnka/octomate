from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from octomate.store import InteractionStore, QuestionResponse
from octomate.tentacles.feelers import ConfirmationFeeler, QuestionFeeler, TodoFeeler

if TYPE_CHECKING:
    from octomate.schemas.actions import ConfirmAction
    from octomate.tentacles.base import SendTarget
    from octomate.tentacles.slack.ink import SlackInk

logger = logging.getLogger(__name__)


class SlackConfirmationFeeler(ConfirmationFeeler):
    ink: SlackInk
    store: InteractionStore

    def __init__(self, ink: SlackInk, store: InteractionStore) -> None:
        self.ink = ink
        self.store = store

    async def send_confirmation(
        self, target: SendTarget, action: ConfirmAction
    ) -> bool:
        channel = str(target.chat_id)
        title = action.title or action.tool_name
        args_json = json.dumps(action.args, ensure_ascii=False, indent=2)
        if len(args_json) > 2000:  # Slack block text limit is 3000 chars
            args_json = args_json[:2000] + "\n… (truncated)"
        description = action.description or action.tool_name

        mention_line = ""
        if action.approvers:
            mentions = " ".join(f"<@{uid}>" for uid in action.approvers)
            mention_line = f"\n{mentions}\n"

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Permission Required"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Tool:* {title}\n"
                        + mention_line
                        + f"*Description:* {description}\n"
                        f"*Arguments:*\n```{args_json}```"
                    ),
                },
            },
            {"type": "divider"},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "action_id": f"confirm_approve_{action.confirmation_id}",
                        "value": json.dumps(
                            {
                                "action": "confirm",
                                "confirmation_id": action.confirmation_id,
                                "approved": "true",
                                "title": title,
                            }
                        ),
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "style": "danger",
                        "action_id": f"confirm_deny_{action.confirmation_id}",
                        "value": json.dumps(
                            {
                                "action": "confirm",
                                "confirmation_id": action.confirmation_id,
                                "approved": "false",
                                "title": title,
                            }
                        ),
                    },
                ],
            },
        ]

        thread_ts = str(target.reply_to) if target.reply_to else None
        ts = await self.ink.send_message(
            channel,
            text="Permission Required",
            blocks=blocks,
            thread_ts=thread_ts,
        )
        return ts is not None


class SlackTodoFeeler(TodoFeeler):
    ink: SlackInk
    store: InteractionStore
    pinned: dict[str, str]  # channel_key -> pinned message ts

    def __init__(self, ink: SlackInk, store: InteractionStore) -> None:
        self.ink = ink
        self.store = store
        self.pinned = {}

    def channel_key(self, target: SendTarget) -> str:
        thread = str(target.reply_to) if target.reply_to else ""
        return f"{target.chat_id}:{thread}"

    async def upsert_todo_list(
        self,
        target: SendTarget,
        items: list,
        existing_ts: str | None = None,
    ) -> str | None:
        channel = str(target.chat_id)
        thread_ts = str(target.reply_to) if target.reply_to else None

        lines: list[str] = []
        for item in items:
            if item.status == "completed":
                icon = ":white_check_mark:"
                text = f"~{item.title}~"
            elif item.status == "cancelled":
                icon = ":x:"
                text = f"~{item.title}~"
            elif item.status == "in_progress":
                icon = ":hourglass_flowing_sand:"
                label = item.active_form or item.title
                text = f"*{label}*"
            else:
                icon = ":radio_button:"
                text = item.title
            lines.append(f"{icon}  {text}")

        body = "\n".join(lines) if lines else "_No tasks_"

        done_count = sum(1 for i in items if i.status in ("completed", "cancelled"))
        in_progress_count = sum(1 for i in items if i.status == "in_progress")
        total = len(items)

        blocks: list[dict] = [
            {"type": "header", "text": {"type": "plain_text", "text": "📋 Task List"}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        ]
        if total:
            parts = [f"*{done_count}/{total}* completed"]
            if in_progress_count:
                parts.append(f"*{in_progress_count}* in progress")
            blocks.append({"type": "divider"})
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": "  ·  ".join(parts)}],
                }
            )

        if existing_ts is None:
            return await self.ink.send_message(
                channel, text="📋 Task List", blocks=blocks, thread_ts=thread_ts
            )
        await self.ink.update_message(
            channel, existing_ts, text="📋 Task List", blocks=blocks
        )
        return existing_ts

    async def pin_todo(self, target: SendTarget, ts: str) -> bool:
        channel = str(target.chat_id)
        key = self.channel_key(target)
        old = self.pinned.get(key)
        if old and old != ts:
            await self.ink.unpin_message(channel, old)
        ok = await self.ink.pin_message(channel, ts)
        if ok:
            self.pinned[key] = ts
        return ok

    async def unpin_todo(self, target: SendTarget, ts: str) -> bool:
        channel = str(target.chat_id)
        key = self.channel_key(target)
        ok = await self.ink.unpin_message(channel, ts)
        if ok:
            self.pinned.pop(key, None)
        return ok


class SlackQuestionFeeler(QuestionFeeler):
    ink: SlackInk
    store: InteractionStore

    def __init__(self, ink: SlackInk, store: InteractionStore) -> None:
        self.ink = ink
        self.store = store

    async def ask_question(
        self,
        target: SendTarget,
        text: str,
        options: list[str] | None = None,
        multi_select: bool = False,
    ) -> QuestionResponse | None:
        question, future = self.store.create_question(text, options)
        channel = str(target.chat_id)

        blocks: list[dict] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Question"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            },
        ]

        if options:
            blocks.append({"type": "divider"})
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": opt},
                            "action_id": f"question_{question.question_id}_{i}",
                            "value": json.dumps(
                                {
                                    "action": "question_answer",
                                    "question_id": question.question_id,
                                    "answer": opt,
                                }
                            ),
                        }
                        for i, opt in enumerate(options)
                    ],
                }
            )
        else:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "_Reply in this thread to answer._",
                    },
                }
            )

        thread_ts = str(target.reply_to) if target.reply_to else None
        ts = await self.ink.send_message(
            channel, text=f"Question: {text}", blocks=blocks, thread_ts=thread_ts
        )
        if not ts:
            self.store.expire_question(question.question_id)
            return None

        try:
            return await asyncio.wait_for(future, timeout=self.store.timeout)
        except TimeoutError:
            self.store.expire_question(question.question_id)
            return None
