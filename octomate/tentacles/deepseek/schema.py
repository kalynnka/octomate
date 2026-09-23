"""Configuration supplied to the DeepSeek harness process."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, RootModel
from typing_extensions import TypedDict


class FileStorageConfig(TypedDict):
    path: Path


class FileStoragePatch(TypedDict):
    id: Literal["settings", "credentials"]
    config: FileStorageConfig


class SessionStorageConfig(TypedDict):
    root: Path


class SessionStoragePatch(TypedDict):
    id: Literal["session-persistence-jsonl"]
    config: SessionStorageConfig


class AttachmentStorageConfig(TypedDict):
    dshHome: Path


class AttachmentStoragePatch(TypedDict):
    id: Literal["attachment-local"]
    config: AttachmentStorageConfig


class SharedDataPatch(
    RootModel[
        list[
            Annotated[
                FileStoragePatch | SessionStoragePatch | AttachmentStoragePatch,
                Field(discriminator="id"),
            ]
        ]
    ]
):
    pass
