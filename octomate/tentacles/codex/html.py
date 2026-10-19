"""Keep explicit visualization references out of user-facing Codex reply text."""

from octomate_protocol.visualizations import (
    VISUALIZATION_REFERENCE,
    VISUALIZATION_START,
)
from octomate_protocol.visualizations import HtmlReference as HtmlReference


def visualization_text(text: str, *, final: bool = False) -> tuple[str, str]:
    """Remove content references, holding incomplete markers across streamed deltas."""
    text = VISUALIZATION_REFERENCE.sub("", text)
    start = text.rfind("\ue200")
    if not final and start >= 0:
        tail = text[start:]
        if VISUALIZATION_START.startswith(tail) or tail.startswith(VISUALIZATION_START):
            return text[:start], tail
    return text, ""
