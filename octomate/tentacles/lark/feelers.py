"""Lark Feelers — interactive card implementations for Lark (Feishu)."""

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
    from octomate.tentacles.lark.ink import LarkInk
    from octomate.transmuters.interactions import Confirmation, Question

logger = logging.getLogger(__name__)


class LarkConfirmationFeeler(ConfirmationFeeler):
    ink: LarkInk

    def __init__(self, ink: LarkInk, store: InteractionStore) -> None:
        super().__init__(store)
        self.ink = ink

    async def send_confirmation(self, key: SessionKey, action: Confirmation) -> bool:
        chat_id = str(key.chat_id or key.group_id or key.user_id)
        receive_id_type = "chat_id" if key.group_id else "open_id"

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
        ts = await self.ink.send_message(chat_id, receive_id_type, "interactive", card)
        if ts:
            self.store.set_card_message_id(action.confirmation_id, ts)
        return True

    async def send_timeout_notification(
        self, key: SessionKey, action: Confirmation
    ) -> None:
        chat_id = str(key.chat_id or key.group_id or key.user_id)
        receive_id_type = "chat_id" if key.group_id else "open_id"
        title = action.title or action.tool_name
        args_json = json.dumps(action.args, ensure_ascii=False, indent=2)
        if len(args_json) > 2000:
            args_json = args_json[:2000] + "\n… (truncated)"

        card = json.dumps(
            {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"{title} — Auto-refused (timed out)",
                    },
                    "template": "grey",
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            f"⏳ **{title}** — Auto-refused (timed out)\n"
                            f"**Arguments:**\n```json\n{args_json}\n```"
                        ),
                    },
                ],
            }
        )
        await self.ink.send_message(chat_id, receive_id_type, "interactive", card)

    async def dismiss_confirmation(
        self, key: SessionKey, action: Confirmation
    ) -> None:
        ts = self.store.card_message_ids.pop(action.confirmation_id, None)
        if not ts:
            return
        title = action.title or action.tool_name
        card = json.dumps(
            {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"{title} \u2014 Cancelled",
                    },
                    "template": "grey",
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": f"\u26a0\ufe0f **{title}** \u2014 Cancelled",
                    },
                ],
            }
        )
        await self.ink.update_message(ts, "interactive", card)


class LarkTodoFeeler(TodoFeeler):
    ink: LarkInk
    pinned: dict[str, str]  # chat_id -> pinned message_id

    def __init__(self, ink: LarkInk, store: InteractionStore) -> None:
        super().__init__(store)
        self.ink = ink
        self.pinned = {}

    async def upsert_todo_list(
        self,
        key: SessionKey,
        items: list,
        existing_ts: str | None = None,
    ) -> str | None:
        chat_id = str(key.chat_id or key.group_id or key.user_id)
        receive_id_type = "chat_id" if key.group_id else "open_id"

        lines: list[str] = []
        for item in items:
            if item.status == "completed":
                icon = "✅"
                text = f"~~{item.title}~~"
            elif item.status == "cancelled":
                icon = "❌"
                text = f"~~{item.title}~~"
            elif item.status == "in_progress":
                icon = "⏳"
                label = item.active_form or item.title
                text = f"**{label}**"
            else:
                icon = "⬜"
                text = item.title
            lines.append(f"{icon}  {text}")

        body = "\n".join(lines) if lines else "_No tasks_"

        done_count = sum(1 for i in items if i.status in ("completed", "cancelled"))
        in_progress_count = sum(1 for i in items if i.status == "in_progress")
        total = len(items)

        elements: list[dict] = [{"tag": "markdown", "content": body}]
        if total:
            parts = [f"**{done_count}/{total}** completed"]
            if in_progress_count:
                parts.append(f"**{in_progress_count}** in progress")
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": "  ·  ".join(parts)})

        template = "green" if total > 0 and done_count == total else "blue"
        card = json.dumps(
            {
                "header": {
                    "title": {"tag": "plain_text", "content": "📋 Task List"},
                    "template": template,
                },
                "elements": elements,
            }
        )

        if existing_ts is None:
            return await self.ink.send_message(
                chat_id, receive_id_type, "interactive", card
            )
        ok = await self.ink.update_message(existing_ts, "interactive", card)
        return existing_ts if ok else None

    async def pin_todo(self, key: SessionKey, ts: str) -> bool:
        cache_key = str(key.chat_id or key.group_id or key.user_id)
        old = self.pinned.get(cache_key)
        if old and old != ts:
            await self.ink.unpin_message(old)
        ok = await self.ink.pin_message(ts)
        if ok:
            self.pinned[cache_key] = ts
        return ok

    async def unpin_todo(self, key: SessionKey, ts: str) -> bool:
        cache_key = str(key.chat_id or key.group_id or key.user_id)
        ok = await self.ink.unpin_message(ts)
        if ok:
            self.pinned.pop(cache_key, None)
        return ok


class LarkQuestionFeeler(QuestionFeeler):
    ink: LarkInk

    def __init__(self, ink: LarkInk, store: InteractionStore) -> None:
        super().__init__(store)
        self.ink = ink

    async def ask_question(
        self,
        key: SessionKey,
        text: str,
        session_key: SessionKey,
        options: list[str] | None = None,
        multi_select: bool = False,
    ) -> QuestionResponse | None:
        question, future = await self.create_question(session_key, text, options)
        chat_id = str(key.chat_id or key.group_id or key.user_id)
        receive_id_type = "chat_id" if key.group_id else "open_id"

        elements: list[dict] = [{"tag": "markdown", "content": text}]
        if options:
            elements.append({"tag": "hr"})
            if multi_select:
                options_md = "\n".join(f"- {opt}" for opt in options)
                elements.append({"tag": "markdown", "content": options_md})
            else:
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
        if multi_select:
            input_placeholder = (
                "Type selected options (comma-separated) or your own answer..."
            )
        elif options:
            input_placeholder = "Or type your own answer..."
        else:
            input_placeholder = "Type your answer..."
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
                            "content": input_placeholder,
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
            await self.expire_question(question.question_id)
            return None

        self.store.set_card_message_id(question.question_id, sent)

        try:
            return await asyncio.wait_for(future, timeout=self.timeout)
        except TimeoutError:
            await self.expire_question(question.question_id)
            return None

    async def dismiss_question(self, key: SessionKey, question: Question) -> None:
        ts = self.store.card_message_ids.pop(question.question_id, None)
        if not ts:
            return
        card = json.dumps(
            {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": "Question \u2014 Cancelled",
                    },
                    "template": "grey",
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": "\u26a0\ufe0f **Question** \u2014 Cancelled",
                    },
                ],
            }
        )
        await self.ink.update_message(ts, "interactive", card)
