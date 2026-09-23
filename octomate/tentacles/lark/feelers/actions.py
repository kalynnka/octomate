"""The `action` values Lark card buttons carry back to the tentacle."""

from __future__ import annotations

from enum import StrEnum


class LarkCardAction(StrEnum):
    """The `action` a card button's value names, dispatched on in `on_card_action`."""

    APPROVAL_APPROVE = "approval_approve"
    APPROVAL_DENY = "approval_deny"
    ASK_QUESTION_BACK = "ask_question_back"
    ASK_QUESTION_NEXT = "ask_question_next"
    ASK_QUESTION_SUBMIT = "ask_question_submit"
    ASK_QUESTION_CHOICE = "ask_question_choice"
