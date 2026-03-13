from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Annotated, Any, Literal, NotRequired, Union

from pydantic import BaseModel, Discriminator, Field, field_validator
from pydantic_ai import BinaryContent
from pydantic_ai.messages import UserContent
from typing_extensions import TypedDict


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
    sub_type: int | None = None

    @field_validator("file", mode="before")
    @classmethod
    def _coerce_path(cls, v: Any) -> str:
        if isinstance(v, Path):
            return str(v.resolve())
        return v

    @property
    def path(self) -> Path:
        return Path(self.file)


class ReplyData(TypedDict):
    id: str


class FaceData(TypedDict):
    id: str


class RecordData(TypedDict):
    file: str
    url: NotRequired[str | None]


class VideoData(TypedDict):
    file: str
    url: NotRequired[str | None]


class PokeData(TypedDict):
    type: str
    id: str
    name: NotRequired[str | None]


class AnonymousData(TypedDict, total=False):
    ignore: int | None


class ShareData(TypedDict):
    url: str
    title: str
    content: NotRequired[str | None]
    image: NotRequired[str | None]


class ContactData(TypedDict):
    type: Literal["qq", "group"]
    id: str


class LocationData(TypedDict):
    lat: str
    lon: str
    title: NotRequired[str | None]
    content: NotRequired[str | None]


class MusicData(TypedDict):
    type: Literal["qq", "163", "xm", "custom"]
    id: NotRequired[str | None]
    url: NotRequired[str | None]
    audio: NotRequired[str | None]
    title: NotRequired[str | None]
    content: NotRequired[str | None]
    image: NotRequired[str | None]


class ForwardData(TypedDict):
    id: str


class NodeData(TypedDict, total=False):
    id: str | None
    user_id: str | None
    nickname: str | None
    content: str | None


class XmlData(TypedDict):
    content: str


class JsonData(TypedDict):
    content: str


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


class ReplySegment(Segment):
    """Quote/reply to a previous message. Set data.id to the message ID (as string). Must be the first segment in the message."""

    type: Literal["reply"] = "reply"
    data: ReplyData

    def __str__(self) -> str:
        return f"[reply: {self.data['id']}]"


class FaceSegment(Segment):
    type: Literal["face"] = "face"
    data: FaceData

    def __str__(self) -> str:
        return f"[face: {self.data['id']}]"


class RecordSegment(Segment):
    type: Literal["record"] = "record"
    data: RecordData

    def __str__(self) -> str:
        return "[audio]"


class VideoSegment(Segment):
    type: Literal["video"] = "video"
    data: VideoData

    def __str__(self) -> str:
        return "[video]"


class RpsSegment(Segment):
    type: Literal["rps"] = "rps"
    data: dict[str, Any] = Field(default_factory=dict)


class DiceSegment(Segment):
    type: Literal["dice"] = "dice"
    data: dict[str, Any] = Field(default_factory=dict)


class ShakeSegment(Segment):
    type: Literal["shake"] = "shake"
    data: dict[str, Any] = Field(default_factory=dict)


class PokeSegment(Segment):
    type: Literal["poke"] = "poke"
    data: PokeData


class AnonymousSegment(Segment):
    type: Literal["anonymous"] = "anonymous"
    data: AnonymousData = Field(default_factory=AnonymousData)


class ShareSegment(Segment):
    type: Literal["share"] = "share"
    data: ShareData

    def __str__(self) -> str:
        return f"[share: {self.data['title']} {self.data['url']}]"


class ContactSegment(Segment):
    type: Literal["contact"] = "contact"
    data: ContactData

    def __str__(self) -> str:
        return f"[contact: {self.data['type']}:{self.data['id']}]"


class LocationSegment(Segment):
    type: Literal["location"] = "location"
    data: LocationData

    def __str__(self) -> str:
        label = self.data.get("title") or f"({self.data['lat']},{self.data['lon']})"
        return f"[location: {label}]"


class MusicSegment(Segment):
    type: Literal["music"] = "music"
    data: MusicData

    def __str__(self) -> str:
        label = self.data.get("title") or self.data["type"]
        return f"[music: {label}]"


class ForwardSegment(Segment):
    type: Literal["forward"] = "forward"
    data: ForwardData

    def __str__(self) -> str:
        return f"[forward: {self.data['id']}]"


class NodeSegment(Segment):
    type: Literal["node"] = "node"
    data: NodeData


class XmlSegment(Segment):
    type: Literal["xml"] = "xml"
    data: XmlData


class JsonSegment(Segment):
    type: Literal["json"] = "json"
    data: JsonData


MessageSegment = Annotated[
    Union[
        TextSegment,
        AtSegment,
        ImageSegment,
        ReplySegment,
        FaceSegment,
        RecordSegment,
        VideoSegment,
        RpsSegment,
        DiceSegment,
        ShakeSegment,
        PokeSegment,
        AnonymousSegment,
        ShareSegment,
        ContactSegment,
        LocationSegment,
        MusicSegment,
        ForwardSegment,
        NodeSegment,
        XmlSegment,
        JsonSegment,
    ],
    Discriminator("type"),
]

AgentSegment = Annotated[
    Union[TextSegment, ImageSegment, AtSegment, ReplySegment],
    Discriminator("type"),
]
