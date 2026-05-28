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
DeferredActionKind = Literal["call", "approval"]
DeferredActionStatus = Literal[
    "pending",
    "answered",
    "approved",
    "denied",
    "expired",
    "failed",
]
