"""Metadata for files whose contents live in a storage provider."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import ClassVar

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import BigInteger, CheckConstraint, String, UniqueConstraint, Uuid, case
from sqlalchemy.orm import Mapped, column_property, mapped_column
from uuid_utils.compat import uuid7

from octomate.models.base import Base, MapperArgs, UTCDateTime
from octomate.types.files import FileProviderName


class File(Base, TransmuterProxiedMixin):
    """One stored file, identified independently of its name and backend."""

    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("provider", "key", name="uq_files_provider_key"),
        CheckConstraint("size >= 0", name="ck_files_size_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Filename including its extension, without directories.",
    )
    media_type: Mapped[str] = mapped_column(
        String, nullable=False, comment="MIME type identifying the file subtype."
    )
    size: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="Size of the stored content in bytes."
    )
    provider: Mapped[FileProviderName] = mapped_column(
        String,
        nullable=False,
        comment="Stable deployment name of the storage provider.",
    )
    key: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Opaque content key within the storage provider.",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="When this file was created, in UTC.",
    )

    mime_kind: Mapped[str] = column_property(
        case(
            (
                media_type.in_(
                    (
                        "application/octet-stream",
                        "text/plain",
                        "text/markdown",
                        "application/json",
                        "application/jsonl",
                        "image/gif",
                        "image/png",
                        "image/jpeg",
                    )
                ),
                media_type,
            ),
            else_="file",
        ),
        doc="Computed subtype discriminator that preserves the original MIME type.",
    )
    __mapper_args__: ClassVar[MapperArgs] = {
        "polymorphic_on": mime_kind,
        "polymorphic_identity": "file",
    }


class Binary(File):
    """Unclassified binary content."""

    __mapper_args__: ClassVar[MapperArgs] = {
        "polymorphic_identity": "application/octet-stream"
    }


class Text(File):
    """A plain text file."""

    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_identity": "text/plain"}


class Markdown(File):
    """A Markdown document."""

    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_identity": "text/markdown"}


class Json(File):
    """A JSON document."""

    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_identity": "application/json"}


class Jsonl(File):
    """A file of newline-separated JSON records."""

    __mapper_args__: ClassVar[MapperArgs] = {
        "polymorphic_identity": "application/jsonl"
    }


class Image(File):
    """The queryable parent of supported image formats."""

    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_abstract": True}


class Gif(Image):
    """A GIF image."""

    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_identity": "image/gif"}


class Png(Image):
    """A PNG image."""

    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_identity": "image/png"}


class Jpeg(Image):
    """A JPEG image, including files with a .jpg extension."""

    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_identity": "image/jpeg"}
