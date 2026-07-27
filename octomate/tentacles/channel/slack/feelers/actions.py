from __future__ import annotations

from enum import StrEnum


class SlackBlockAction(StrEnum):
    APPROVAL_APPROVE = "octomate_approval_approve"
    APPROVAL_DENY = "octomate_approval_deny"
    ASK_QUESTION_BACK = "octomate_question_back"
    ASK_QUESTION_NEXT = "octomate_question_next"
    ASK_QUESTION_SUBMIT = "octomate_question_submit"
    ASK_QUESTION_CHOICE = "octomate_question_choice"
    ASK_QUESTION_ANSWER = "octomate_question_answer"
    OAUTH_OPEN = "octomate_oauth_open"
    OAUTH_CONFIRM = "octomate_oauth_confirm"
