from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Annotated, Literal, NotRequired

from pydantic import BaseModel, Discriminator, field_validator
from pydantic_ai import BinaryContent
from pydantic_ai.messages import UserContent
from typing_extensions import TypedDict

from octomate.types.json import JsonObject


class TextData(TypedDict):
    text: str


class AtData(BaseModel):
    user_id: str
    name: str | None = None


class ImageData(BaseModel):
    """Image payload. `file` is always a local file path (str).

    Inbound images are downloaded to local storage by the tentacle layer.
    Outbound images are read from local files and uploaded by the tentacle.
    Use the `path` property to get it as a `Path` object.
    """

    file: str
    url: str | None = None
    name: str | None = None
    summary: str | None = None

    @field_validator("file", mode="before")
    @classmethod
    def _coerce_path(cls, value: str | Path) -> str:
        if isinstance(value, Path):
            return str(value.resolve())
        return value

    @property
    def path(self) -> Path:
        return Path(self.file)


class MarkdownData(TypedDict):
    text: str


class ReplyData(TypedDict):
    id: str
    content: NotRequired[str]


class FileData(BaseModel):
    file: str
    name: str = ""
    size: int = 0


class CardData(BaseModel):
    payload: JsonObject


class Segment(BaseModel):
    """Base class for all message segments."""

    def __str__(self) -> str:
        return f"[{getattr(self, 'type', 'unknown')}]"

    def to_content(self) -> UserContent:
        return str(self)


class TextSegment(Segment):
    """Plain text content. Set data.text to the message text."""

    type: Literal["text"] = "text"
    data: TextData

    def __str__(self) -> str:
        return self.data["text"]


class AtSegment(Segment):
    """Mention/at a user. Set data.user_id to the user's platform ID (as string). Optionally set data.name."""

    type: Literal["at"] = "at"
    data: AtData

    def __str__(self) -> str:
        return f"@{self.data.name or self.data.user_id}"


class ImageSegment(Segment):
    """Send an image. Set data.file to a local file path.
    Never use http/https URLs — the system manages file storage automatically."""

    type: Literal["image"] = "image"
    data: ImageData

    def __str__(self) -> str:
        label = self.data.summary or self.data.name or "image"
        return f"[image: {label} | {self.data.file}]"

    def to_content(self) -> UserContent:
        path = self.data.path
        if path.exists():
            media_type = mimetypes.guess_type(path.name)[0] or "image/png"
            return BinaryContent(data=path.read_bytes(), media_type=media_type)
        return str(self)


class MarkdownSegment(Segment):
    """Markdown-formatted text. Set data.text to the markdown content.
    Platforms that support markdown render it natively; others strip it to plain text.
    Images can be embedded inline using ![alt](image_key) syntax — no separate ImageSegment needed."""

    type: Literal["markdown"] = "markdown"
    data: MarkdownData

    def __str__(self) -> str:
        return self.data["text"]


class ReplySegment(Segment):
    """Quote/reply to a previous message so the answer threads onto it. Set data.id
    to the target's message id — the `#msg:<id>` handle shown beside each message in
    the conversation. Put it first; the message threads onto the first reply it carries."""

    type: Literal["reply"] = "reply"
    data: ReplyData

    def __str__(self) -> str:
        content = self.data.get("content")
        if content:
            return f"[reply: {self.data['id']}] {content}"
        return f"[reply: {self.data['id']}]"


class FileSegment(Segment):
    type: Literal["file"] = "file"
    data: FileData

    def __str__(self) -> str:
        return f"[file: {self.data.name or self.data.file}]"


class CardSegment(Segment):
    type: Literal["card"] = "card"
    data: CardData

    def __str__(self) -> str:
        return "[card]"


MessageSegment = Annotated[
    TextSegment
    | AtSegment
    | ImageSegment
    | MarkdownSegment
    | ReplySegment
    | FileSegment
    | CardSegment,
    Discriminator("type"),
]
