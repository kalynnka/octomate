"""Capture explicit Codex visualization references from a driven run's workspace.

Only complete ``visualize`` content references are captured, never HTML fences
or ordinary file links. Files must be regular UTF-8 .html files beneath the run
workspace, with no symlink components, and at most 1 MB. The saved snapshot needs
no access to the source file when viewed. Host-specific widget APIs are not supplied.
"""

import os
import re
import stat
from contextlib import ExitStack
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

VISUALIZATION_START = "\ue200visualize\ue202"
VISUALIZATION_REFERENCE = re.compile(r"\ue200visualize\ue202([^\ue201]*)\ue201")


class HtmlReference(BaseModel):
    """An explicit file reference with an optional layout hint retained on capture."""

    path: Path = Field(description="Absolute HTML path in this run's workspace.")
    title: str | None = Field(default=None, min_length=1, max_length=200)
    mode: Literal["wide"] | None = Field(
        default=None,
        description="Native layout hint requesting a wider inline visualization.",
    )

    def read(self, workspace: Path) -> str:
        """Read a bounded snapshot through directory descriptors without following links."""
        relative = self.path.relative_to(workspace)
        if ".." in relative.parts or relative.suffix.lower() != ".html":
            raise ValueError("Visualization must be an HTML file inside the workspace")
        with ExitStack() as stack:
            directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
            stack.callback(os.close, directory)
            for part in relative.parts[:-1]:
                directory = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                stack.callback(os.close, directory)
            descriptor = os.open(
                relative.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
            source = stack.enter_context(os.fdopen(descriptor, "rb"))
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("Visualization must be a regular file")
            data = source.read(1_000_001)
        if len(data) > 1_000_000:
            raise ValueError("Visualization exceeds 1 MB")
        html = data.decode("utf-8")
        if not html.strip():
            raise ValueError("Visualization must be nonempty")
        return html


def visualization_text(text: str, *, final: bool = False) -> tuple[str, str]:
    """Remove content references, holding incomplete markers across streamed deltas."""
    text = VISUALIZATION_REFERENCE.sub("", text)
    start = text.rfind("\ue200")
    if not final and start >= 0:
        tail = text[start:]
        if VISUALIZATION_START.startswith(tail) or tail.startswith(VISUALIZATION_START):
            return text[:start], tail
    return text, ""
