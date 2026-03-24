from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from octomate.store import InteractionStore, QuestionResponse, TodoItem
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

    def __init__(self, ink: SlackInk, store: InteractionStore) -> None:
        self.ink = ink
        self.store = store

    async def create_todo(
        self, target: SendTarget, title: str, assignee: str | None = None
    ) -> TodoItem | None:
        item = self.store.create_todo(title, assignee)
        channel = str(target.chat_id)

        assignee_line = f"\n*Assignee:* <@{assignee}>" if assignee else ""
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "TODO"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":white_square: {title}" + assignee_line,
                },
            },
            {"type": "divider"},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Done"},
                        "style": "primary",
                        "action_id": f"todo_done_{item.todo_id}",
                        "value": json.dumps(
                            {
                                "action": "todo_update",
                                "todo_id": item.todo_id,
                                "title": title,
                                "status": "done",
                            }
                        ),
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "style": "danger",
                        "action_id": f"todo_cancel_{item.todo_id}",
                        "value": json.dumps(
                            {
                                "action": "todo_update",
                                "todo_id": item.todo_id,
                                "title": title,
                                "status": "cancelled",
                            }
                        ),
                    },
                ],
            },
        ]

        thread_ts = str(target.reply_to) if target.reply_to else None
        ts = await self.ink.send_message(
            channel, text=f"TODO: {title}", blocks=blocks, thread_ts=thread_ts
        )
        return item if ts else None

    async def update_todo(self, todo_id: str, status: str) -> bool:
        return self.store.update_todo(todo_id, status)


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
