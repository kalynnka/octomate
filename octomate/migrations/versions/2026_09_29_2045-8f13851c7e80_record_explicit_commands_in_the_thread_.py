"""Keep explicit command deliveries out of ordinary chat prompts.

Command receipts share the ledger delivery key and retain typed intent and outcomes.
Existing rows remain messages. SQLite batch recreation must retain the ledger's
search triggers so history indexing continues after either migration direction.

Revision ID: 8f13851c7e80
Revises: cfecde28ca54
Create Date: 2026-09-29 20:45:41.201675

"""

from collections.abc import Generator, Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f13851c7e80"
down_revision: str | Sequence[str] | None = "cfecde28ca54"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


@contextmanager
def preserve_ledger_triggers() -> Generator[None]:
    """Batch table recreation drops SQLite triggers; retain their exact definitions."""
    triggers = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'thread_messages'"
            )
        )
        .scalars()
        .all()
    )
    yield
    for trigger in triggers:
        op.execute(trigger)


def upgrade() -> None:
    """Upgrade schema."""
    with (
        preserve_ledger_triggers(),
        op.batch_alter_table("thread_messages", schema=None) as batch_op,
    ):
        batch_op.add_column(
            sa.Column(
                "kind",
                sa.String(),
                server_default="message",
                nullable=False,
                comment="Whether this ledger row is a chat message or an explicit command.",
            )
        )
        batch_op.add_column(
            sa.Column(
                "conversation_id",
                sa.Uuid(),
                nullable=True,
                comment="The command's target conversation; NULL after that conversation is deleted.",
            )
        )
        batch_op.add_column(
            sa.Column(
                "invocation",
                sa.JSON(),
                nullable=True,
                comment="Explicit command intent with raw arguments and resolved attachments.",
            )
        )
        batch_op.add_column(
            sa.Column(
                "outcome",
                sa.JSON(none_as_null=True),
                nullable=True,
                comment="Recorded command outcome; NULL means no terminal outcome was recorded, not permission to retry.",
            )
        )
        batch_op.create_foreign_key(
            "fk_thread_command_conversation",
            "conversations",
            ["conversation_id"],
            ["id"],
            ondelete="SET NULL",
            use_alter=True,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with (
        preserve_ledger_triggers(),
        op.batch_alter_table("thread_messages", schema=None) as batch_op,
    ):
        batch_op.drop_constraint("fk_thread_command_conversation", type_="foreignkey")
        batch_op.drop_column("outcome")
        batch_op.drop_column("invocation")
        batch_op.drop_column("conversation_id")
        batch_op.drop_column("kind")
