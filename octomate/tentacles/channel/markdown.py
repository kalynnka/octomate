from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class MarkdownChunker:
    DEFAULT_LIMIT: ClassVar[int] = 12_000

    limit: int = DEFAULT_LIMIT
    natural_min_size: int | None = None

    @property
    def effective_natural_min_size(self) -> int:
        if self.natural_min_size is not None:
            return self.natural_min_size
        return self.limit // 2

    def chunk(self, text: str) -> list[str]:
        if not text:
            return [""]

        chunks: list[str] = []
        remaining = text
        while len(remaining) > self.limit:
            split_at = self.split_index(remaining)
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]
        if remaining:
            chunks.append(remaining)
        return chunks

    def split_index(self, text: str) -> int:
        for boundary in (
            self.last_separator_boundary(text, "\n\n"),
            self.last_separator_boundary(text, "\n"),
            self.last_sentence_boundary(text),
            self.last_whitespace_boundary(text),
        ):
            if boundary >= self.effective_natural_min_size:
                return boundary

        return self.last_whitespace_boundary(text) or self.limit

    def last_separator_boundary(self, text: str, separator: str) -> int:
        boundary = 0
        start = 0
        while True:
            index = text.find(separator, start, self.limit)
            if index < 0:
                return boundary
            candidate = index + len(separator)
            if candidate <= self.limit:
                boundary = candidate
            start = index + len(separator)

    def last_sentence_boundary(self, text: str) -> int:
        boundary = 0
        for match in re.finditer(r'[.!?][)"\']?\s+', text[: self.limit]):
            boundary = match.end()
        return boundary

    def last_whitespace_boundary(self, text: str) -> int:
        boundary = 0
        for match in re.finditer(r"\s+", text[: self.limit]):
            boundary = match.end()
        return boundary
