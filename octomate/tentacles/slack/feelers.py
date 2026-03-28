from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from octomate.stores.interaction import InteractionStore
from octomate.tentacles.feelers import (
    ConfirmationFeeler,
    QuestionFeeler,
    QuestionResponse,
    TodoFeeler,
)

if TYPE_CHECKING:
    from octomate.schemas.session import SessionKey
    from octomate.tentacles.base import SendTarget
    from octomate.tentacles.slack.ink import SlackInk
    from octomate.transmuters.interactions import Confirmation, Question

logger = logging.getLogger(__name__)


class SlackConfirmationFeeler(ConfirmationFeeler):
    ink: SlackInk

    def __init__(self, ink: SlackInk, store: InteractionStore) -> None:
        super().__init__(store)
        self.ink = ink

    async def send_confirmation(self, target: SendTarget, action: Confirmation) -> bool:
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
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Allow in session"},
                        "action_id": f"confirm_allow_session_{action.confirmation_id}",
                        "value": json.dumps(
                            {
                                "action": "confirm_allow_session",
                                "confirmation_id": action.confirmation_id,
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
        if ts:
            self.store.set_card_message_id(action.confirmation_id, ts)
        return ts is not None

    async def send_timeout_notification(
        self, target: SendTarget, action: Confirmation
    ) -> None:
        channel = str(target.chat_id)
        thread_ts = str(target.reply_to) if target.reply_to else None
        title = action.title or action.tool_name
        args_json = json.dumps(action.args, ensure_ascii=False, indent=2)
        if len(args_json) > 2000:
            args_json = args_json[:2000] + "\n… (truncated)"
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":hourglass_flowing_sand: *{title}* — Auto-refused (timed out)\n"
                        f"*Arguments:*\n```{args_json}```"
                    ),
                },
            }
        ]
        await self.ink.send_message(
            channel,
            text=f"{title} — Auto-refused (timed out)",
            blocks=blocks,
            thread_ts=thread_ts,
        )

    async def dismiss_confirmation(
        self, target: SendTarget, action: Confirmation
    ) -> None:
        ts = self.store.card_message_ids.pop(action.confirmation_id, None)
        if not ts:
            return
        channel = str(target.chat_id)
        title = action.title or action.tool_name
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"⚠️ *{title}* — Cancelled",
                },
            }
        ]
        await self.ink.update_message(
            channel, ts, text=f"{title} — Cancelled", blocks=blocks
        )


class SlackTodoFeeler(TodoFeeler):
    ink: SlackInk
    pinned: dict[str, str]  # channel_key -> pinned message ts

    def __init__(self, ink: SlackInk, store: InteractionStore) -> None:
        super().__init__(store)
        self.ink = ink
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
        # Unpin any previously pinned todo for this channel/thread first
        if prev_ts := self.pinned.get(key):
            await self.ink.unpin_message(channel, prev_ts)
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

    def __init__(self, ink: SlackInk, store: InteractionStore) -> None:
        super().__init__(store)
        self.ink = ink

    async def ask_question(
        self,
        target: SendTarget,
        text: str,
        session_key: SessionKey,
        options: list[str] | None = None,
        multi_select: bool = False,
    ) -> QuestionResponse | None:
        question, future = await self.create_question(session_key, text, options)
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
            if multi_select:
                blocks.append(
                    {
                        "type": "actions",
                        "block_id": f"question_cb_block_{question.question_id}",
                        "elements": [
                            {
                                "type": "checkboxes",
                                "action_id": f"question_checkboxes_{question.question_id}",
                                "options": [
                                    {
                                        "text": {"type": "plain_text", "text": opt},
                                        "value": opt,
                                    }
                                    for opt in options
                                ],
                            }
                        ],
                    }
                )
                blocks.append(
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Submit"},
                                "style": "primary",
                                "action_id": f"question_submit_{question.question_id}",
                                "value": json.dumps(
                                    {
                                        "action": "question_submit",
                                        "question_id": question.question_id,
                                    }
                                ),
                            }
                        ],
                    }
                )
            else:
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
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "✏️ Custom Answer",
                            "emoji": True,
                        },
                        "action_id": f"question_custom_{question.question_id}",
                        "value": json.dumps(
                            {
                                "action": "question_custom_answer",
                                "question_id": question.question_id,
                            }
                        ),
                    }
                ],
            }
        )

        thread_ts = str(target.reply_to) if target.reply_to else None
        ts = await self.ink.send_message(
            channel, text=f"Question: {text}", blocks=blocks, thread_ts=thread_ts
        )
        if not ts:
            await self.expire_question(question.question_id)
            return None

        self.store.set_card_message_id(question.question_id, ts)

        try:
            return await asyncio.wait_for(future, timeout=self.timeout)
        except TimeoutError:
            await self.expire_question(question.question_id)
            return None

    async def dismiss_question(self, target: SendTarget, question: Question) -> None:
        ts = self.store.card_message_ids.pop(question.question_id, None)
        if not ts:
            return
        channel = str(target.chat_id)
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "⚠️ *Question* — Cancelled",
                },
            }
        ]
        await self.ink.update_message(
            channel, ts, text="Question — Cancelled", blocks=blocks
        )
