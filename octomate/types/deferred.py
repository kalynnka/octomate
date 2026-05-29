from __future__ import annotations

from typing import Literal

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
