"""rename prompt source names

Revision ID: c1d2e3f4a5b6
Revises: a9b1db7f3dff
Create Date: 2026-06-26 03:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "a9b1db7f3dff"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index("ix_threads_prompt_cursor_message_id", table_name="threads")
    with op.batch_alter_table("threads") as batch_op:
        batch_op.alter_column(
            "prompt_cursor_message_id",
            new_column_name="source_cursor_message_id",
            existing_type=sa.Uuid(),
            existing_nullable=True,
        )
    op.create_index(
        "ix_threads_source_cursor_message_id",
        "threads",
        ["source_cursor_message_id"],
    )

    op.execute(
        "UPDATE message_binding "
        "SET kind = 'request_source' "
        "WHERE kind = 'prompt_source'"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "UPDATE message_binding "
        "SET kind = 'prompt_source' "
        "WHERE kind = 'request_source'"
    )

    op.drop_index("ix_threads_source_cursor_message_id", table_name="threads")
    with op.batch_alter_table("threads") as batch_op:
        batch_op.alter_column(
            "source_cursor_message_id",
            new_column_name="prompt_cursor_message_id",
            existing_type=sa.Uuid(),
            existing_nullable=True,
        )
    op.create_index(
        "ix_threads_prompt_cursor_message_id",
        "threads",
        ["prompt_cursor_message_id"],
    )
