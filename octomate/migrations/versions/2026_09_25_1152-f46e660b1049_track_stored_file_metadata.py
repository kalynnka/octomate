"""Track file metadata with MIME types selecting single-table subtypes.

Revision ID: f46e660b1049
Revises: 9c5cd2f0c70d
Create Date: 2026-09-25 12:22:56.627085

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from octomate.models.base import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = "f46e660b1049"
down_revision: str | Sequence[str] | None = "9c5cd2f0c70d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "name",
            sa.String(),
            nullable=False,
            comment="Filename including its extension, without directories.",
        ),
        sa.Column(
            "media_type",
            sa.String(),
            nullable=False,
            comment="MIME type identifying the file subtype.",
        ),
        sa.Column(
            "size",
            sa.BigInteger(),
            nullable=False,
            comment="Size of the stored content in bytes.",
        ),
        sa.Column(
            "provider",
            sa.String(),
            nullable=False,
            comment="Stable deployment name of the storage provider.",
        ),
        sa.Column(
            "key",
            sa.String(),
            nullable=False,
            comment="Opaque content key within the storage provider.",
        ),
        sa.Column(
            "created_at",
            UTCDateTime(timezone=True),
            nullable=False,
            comment="When this file was created, in UTC.",
        ),
        sa.CheckConstraint("size >= 0", name="ck_files_size_nonnegative"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "key", name="uq_files_provider_key"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("files")
