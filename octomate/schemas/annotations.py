"""Optional source context and file references carried with cleaned message text."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema


class FileReference(BaseModel):
    path: str
    roles: list[str]
    start_line: int | None = None
    end_line: int | None = None
    note: str | None = None


class MessageContext(BaseModel):
    title: str
    text: str
    rollout_ids: list[str] = Field(default_factory=list)


class MessageAnnotations(BaseModel):
    raw: str = Field(
        description="Original harness text before separating injected context or citations."
    )
    source: Literal["codex", "claude"]
    contexts: list[MessageContext] = Field(default_factory=list)
    references: list[FileReference] = Field(default_factory=list)

    def reference(
        self,
        path: str,
        role: str,
        start: int | None = None,
        end: int | None = None,
        note: str | None = None,
    ) -> None:
        for reference in self.references:
            if (reference.path, reference.start_line, reference.end_line) == (
                path,
                start,
                end,
            ):
                if role not in reference.roles:
                    reference.roles.append(role)
                return
        self.references.append(
            FileReference(
                path=path, roles=[role], start_line=start, end_line=end, note=note
            )
        )


InternalMessageAnnotations = SkipJsonSchema[
    MessageAnnotations
]  # Harness context, omitted from tool schemas.
