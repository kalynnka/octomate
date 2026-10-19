"""Explicit Codex HTML references shared by driven capture and native clients.

Files must be regular UTF-8 HTML beneath the session workspace, without symlinks,
and at most 1 MB. Native clients send snapshots alongside the original rollout
line; the server never opens a path supplied by a remote client.
"""

import os
import re
import stat
from contextlib import ExitStack
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_validator,
)

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


class HtmlSnapshot(BaseModel):
    """An explicit visualization and the UTF-8 HTML read on its owning machine."""

    reference: HtmlReference = Field(
        description="The assistant's visualization reference."
    )
    html: str = Field(
        min_length=1,
        max_length=1_000_000,
        description="Captured HTML, at most 1 MB encoded as UTF-8.",
    )

    @field_validator("html")
    @classmethod
    def bounded_html(cls, value: str) -> str:
        if not value.strip() or len(value.encode("utf-8")) > 1_000_000:
            raise ValueError("Visualization must be nonempty UTF-8 HTML at most 1 MB")
        return value


rollout_object_adapter = TypeAdapter(dict[str, JsonValue])


def codex_html_references(raw: str) -> list[HtmlReference]:
    """Read explicit references from assistant response items only.

    Unmodeled lines remain the server tailer's concern. A recognized visualization
    must validate before its client opens any file. Prompts and tool output never
    authorize file capture.
    """
    try:
        line = rollout_object_adapter.validate_json(raw)
    except ValidationError:
        return []
    payload = line.get("payload")
    if line.get("type") != "response_item" or not isinstance(payload, dict):
        return []
    if payload.get("type") != "message" or payload.get("role") != "assistant":
        return []
    content = payload.get("content")
    if not isinstance(content, list):
        return []
    texts = [part.get("text") for part in content if isinstance(part, dict)]
    references = [
        HtmlReference.model_validate_json(match[1])
        for text in texts
        if isinstance(text, str)
        for match in VISUALIZATION_REFERENCE.finditer(text)
    ]
    unique: dict[Path, HtmlReference] = {}
    for reference in references:
        unique.setdefault(reference.path, reference)
    return list(unique.values())
