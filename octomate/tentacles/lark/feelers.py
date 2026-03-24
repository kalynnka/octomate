"""Lark Feelers — interactive card implementations for Lark (Feishu)."""

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
    from octomate.tentacles.lark.ink import LarkInk

logger = logging.getLogger(__name__)


class LarkConfirmationFeeler(ConfirmationFeeler):
    ink: LarkInk
    store: InteractionStore

    def __init__(self, ink: LarkInk, store: InteractionStore) -> None:
        self.ink = ink
        self.store = store

    async def send_confirmation(
        self, target: SendTarget, action: ConfirmAction
    ) -> bool:
        chat_id = str(target.chat_id)
        receive_id_type = "chat_id" if target.chat_type == "group" else "open_id"

        title = action.title or action.tool_name
        args_json = json.dumps(action.args, ensure_ascii=False, indent=2)
        description = action.description or action.tool_name

        mention_line = ""
        if action.approvers:
            mentions = " ".join(f'<at id="{uid}"></at>' for uid in action.approvers)
            mention_line = f"\n{mentions}"

        card = json.dumps(
            {
                "header": {
                    "title": {"tag": "plain_text", "content": "Permission Required"},
                    "template": "orange",
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            f"**Tool:** {title}\n"
                            + mention_line
                            + f"**Description:** {description}\n"
                            f"**Arguments:**\n```json\n{args_json}\n```"
                        ),
                    },
                    {"tag": "hr"},
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "✅ Approve"},
                                "type": "primary",
                                "value": {
                                    "action": "confirm",
                                    "confirmation_id": action.confirmation_id,
                                    "approved": "true",
                                    "title": title,
                                },
                            },
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "❌ Deny"},
                                "type": "danger",
                                "value": {
                                    "action": "confirm",
                                    "confirmation_id": action.confirmation_id,
                                    "approved": "false",
                                    "title": title,
                                },
                            },
                        ],
                    },
                ],
            }
        )
        await self.ink.send_message(chat_id, receive_id_type, "interactive", card)
        return True


class LarkTodoFeeler(TodoFeeler):
    ink: LarkInk
    store: InteractionStore

    def __init__(self, ink: LarkInk, store: InteractionStore) -> None:
        self.ink = ink
        self.store = store

    async def create_todo(
        self,
        target: SendTarget,
        title: str,
        active_form: str | None = None,
        assignee: str | None = None,
    ) -> TodoItem | None:
        item = self.store.create_todo(title, active_form, assignee)
        chat_id = str(target.chat_id)
        receive_id_type = "chat_id" if target.chat_type == "group" else "open_id"

        assignee_line = f'\n**Assignee:** <at id="{assignee}"></at>' if assignee else ""
        card = json.dumps(
            {
                "header": {
                    "title": {"tag": "plain_text", "content": "📝 TODO"},
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": f"☐ {title}" + assignee_line,
                    },
                    {"tag": "hr"},
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "✅ Done"},
                                "type": "primary",
                                "value": {
                                    "action": "todo_update",
                                    "todo_id": item.todo_id,
                                    "title": title,
                                    "status": "completed",
                                },
                            },
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "❌ Cancel"},
                                "type": "danger",
                                "value": {
                                    "action": "todo_update",
                                    "todo_id": item.todo_id,
                                    "title": title,
                                    "status": "cancelled",
                                },
                            },
                        ],
                    },
                ],
            }
        )
        sent = await self.ink.send_message(
            chat_id, receive_id_type, "interactive", card
        )
        return item if sent else None

    async def update_todo(self, todo_id: str, status: str) -> bool:
        return self.store.update_todo(todo_id, status)


class LarkQuestionFeeler(QuestionFeeler):
    ink: LarkInk
    store: InteractionStore

    def __init__(self, ink: LarkInk, store: InteractionStore) -> None:
        self.ink = ink
        self.store = store

    async def ask_question(
        self,
        target: SendTarget,
        text: str,
        options: list[str] | None = None,
    ) -> QuestionResponse | None:
        question, future = self.store.create_question(text, options)
        chat_id = str(target.chat_id)
        receive_id_type = "chat_id" if target.chat_type == "group" else "open_id"

        elements: list[dict] = [{"tag": "markdown", "content": text}]
        if options:
            elements.append({"tag": "hr"})
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": opt},
                            "type": "default",
                            "value": {
                                "action": "question_answer",
                                "question_id": question.question_id,
                                "answer": opt,
                            },
                        }
                        for opt in options
                    ],
                }
            )
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "form",
                "name": f"question_form_{question.question_id}",
                "elements": [
                    {
                        "tag": "input",
                        "name": "answer",
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "Type your answer...",
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Submit"},
                        "type": "primary",
                        "action_type": "form_submit",
                        "name": "submit",
                        "value": {
                            "action": "question_answer",
                            "question_id": question.question_id,
                        },
                    },
                ],
            }
        )

        card = json.dumps(
            {
                "header": {
                    "title": {"tag": "plain_text", "content": "Question"},
                    "template": "blue",
                },
                "elements": elements,
            }
        )
        sent = await self.ink.send_message(
            chat_id, receive_id_type, "interactive", card
        )
        if not sent:
            self.store.expire_question(question.question_id)
            return None

        try:
            return await asyncio.wait_for(future, timeout=self.store.timeout)
        except TimeoutError:
            self.store.expire_question(question.question_id)
            return None
