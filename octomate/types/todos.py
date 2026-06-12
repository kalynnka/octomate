from __future__ import annotations

from typing import Literal

TodoStatus = Literal["pending", "in_progress", "completed", "blocked"]

# Plaintext status markers shared by the model-facing todo render and the
# user-facing channel checklists.
STATUS_MARKERS: dict[TodoStatus, str] = {
    "pending": "[ ]",
    "in_progress": "[*]",
    "completed": "[x]",
    "blocked": "[!]",
}
