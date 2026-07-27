from __future__ import annotations

from enum import StrEnum


class LarkCardAction(StrEnum):
    APPROVAL_APPROVE = "approval_approve"
    APPROVAL_DENY = "approval_deny"
    ASK_QUESTION_BACK = "ask_question_back"
    ASK_QUESTION_NEXT = "ask_question_next"
    ASK_QUESTION_SUBMIT = "ask_question_submit"
    ASK_QUESTION_CHOICE = "ask_question_choice"
    OAUTH_CONFIRM = "oauth_confirm"
