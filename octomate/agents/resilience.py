"""Retry, loop detection, and self-correction for the Pulse graph runner."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter

logger = logging.getLogger(__name__)

MAX_STEP_RETRIES = 3
LOOP_THRESHOLD = 3


class LoopDetectedError(Exception):
    """Raised when a tool-call loop is detected."""


class ToolCallTracker:
    """Hash-based loop detector.

    Records ``(tool_name, args)`` signatures and flags when the same signature
    appears ``threshold`` or more times, indicating the runner is stuck.
    """

    call_counts: Counter[str]
    threshold: int

    def __init__(self, threshold: int = LOOP_THRESHOLD) -> None:
        self.threshold = threshold
        self.call_counts = Counter()

    @staticmethod
    def signature(tool_name: str, args: dict) -> str:
        payload = json.dumps({"tool": tool_name, "args": args}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def record(self, tool_name: str, args: dict) -> bool:
        """Record a call. Returns ``True`` when a loop is detected."""
        sig = self.signature(tool_name, args)
        self.call_counts[sig] += 1
        if self.call_counts[sig] >= self.threshold:
            logger.warning(
                "Loop detected: tool=%s called %d times with same args (sig=%s)",
                tool_name,
                self.call_counts[sig],
                sig,
            )
            return True
        return False

    def reset(self) -> None:
        self.call_counts.clear()
