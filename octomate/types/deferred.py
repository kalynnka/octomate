"""Status and kind literals for deferred actions and their batches."""

from __future__ import annotations

from typing import Literal

ASK_QUESTIONS_TOOL_NAME = "ask_questions"
# Declared on the deferral so a reader can tell a question from the other things a
# run defers — a `teleport` is one too, and only a human answers this one. Classified
# by kind rather than by tool name, as `TELEPORT_KIND` is.
ASK_DEFERR_KIND = "questions"

DeferredBatchStatus = Literal[
    "pending",
    "resolved",
    "resuming",
    "completed",
    "expired",
    "superseded",
    "failed",
]
DeferredActionKind = Literal["question", "approval"]
DeferredResponseMode = Literal["live", "resume"]
DeferredActionStatus = Literal[
    "pending",
    "answered",
    "approved",
    "denied",
    "expired",
    "failed",
]
DeferredQuestionStatus = Literal[
    "pending",
    "answered",
    "expired",
    "failed",
]
DeferredApprovalStatus = Literal[
    "pending",
    "approved",
    "denied",
    "expired",
    "failed",
]
