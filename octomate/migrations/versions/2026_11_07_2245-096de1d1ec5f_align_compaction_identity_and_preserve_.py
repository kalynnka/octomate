"""Use configured ownership and preserve ordered context membership.

Join the applied compaction and native-ownership histories without rewriting
archive messages. Existing transformations gain owner links with unknown order.


Revision ID: 096de1d1ec5f
Revises: 1f46faa4d027, 792e14ef4f19
Create Date: 2026-11-07 22:45:22.532172

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "096de1d1ec5f"
down_revision: str | Sequence[str] | None = ("1f46faa4d027", "792e14ef4f19")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "conversation_compactions",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "compaction_id",
            sa.Uuid(),
            nullable=False,
            comment="Shared compaction identity; deletion is blocked while a conversation still includes it.",
        ),
        sa.Column(
            "position",
            sa.Integer(),
            nullable=True,
            comment="Zero-based compaction order in this conversation; NULL when the order has not been established.",
        ),
        sa.CheckConstraint(
            "position >= 0", name="ck_conversation_compactions_position"
        ),
        sa.ForeignKeyConstraint(
            ["compaction_id"],
            ["compactions.id"],
            name="fk_conversation_compactions_compaction",
            ondelete="NO ACTION",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("conversation_id", "compaction_id"),
        sa.UniqueConstraint(
            "conversation_id", "position", name="uq_conversation_compactions_position"
        ),
    )
    with op.batch_alter_table("conversation_compactions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_conversation_compactions_compaction_id"),
            ["compaction_id"],
            unique=False,
        )

    op.create_table(
        "compaction_memberships",
        sa.Column("compaction_id", sa.Uuid(), nullable=False),
        sa.Column(
            "stage",
            sa.String(),
            nullable=False,
            comment="Before is the input context; after is the replacement context, including retained messages.",
        ),
        sa.Column(
            "position",
            sa.Integer(),
            nullable=False,
            comment="Zero-based message position within this stage; independent of message timestamps and IDs.",
        ),
        sa.Column(
            "model_message_id",
            sa.Uuid(),
            nullable=False,
            comment="Referenced archive message; its run or compaction ownership is unchanged and deletion is blocked while referenced.",
        ),
        sa.CheckConstraint(
            "stage IN ('before', 'after')", name="ck_compaction_memberships_stage"
        ),
        sa.CheckConstraint("position >= 0", name="ck_compaction_memberships_position"),
        sa.ForeignKeyConstraint(
            ["compaction_id"], ["compactions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["model_message_id"],
            ["model_messages.id"],
            name="fk_compaction_memberships_message",
            ondelete="NO ACTION",
        ),
        sa.PrimaryKeyConstraint("compaction_id", "stage", "position"),
        sa.UniqueConstraint(
            "compaction_id",
            "stage",
            "model_message_id",
            name="uq_compaction_memberships_message",
        ),
    )
    with op.batch_alter_table("compaction_memberships", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_compaction_memberships_model_message_id"),
            ["model_message_id"],
            unique=False,
        )

    with op.batch_alter_table("compactions", schema=None, recreate="never") as batch_op:
        batch_op.add_column(
            sa.Column(
                "membership_complete",
                sa.Boolean(),
                server_default=sa.text("(false)"),
                nullable=False,
                comment="Whether before and after membership describe the complete effective context; false means unknown or partial.",
            )
        )
        batch_op.drop_column("native_id")

    op.execute(
        sa.text(
            "INSERT INTO conversation_compactions (conversation_id, compaction_id) "
            "SELECT conversation_id, id FROM compactions"
        )
    )


def downgrade() -> None:
    """Refuse to discard known context membership or inherited history."""
    has_context = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM compaction_memberships) "
            "OR EXISTS (SELECT 1 FROM compactions WHERE membership_complete) "
            "OR EXISTS ("
            "SELECT 1 FROM conversation_compactions AS history "
            "JOIN compactions ON compactions.id = history.compaction_id "
            "WHERE history.position IS NOT NULL "
            "OR history.conversation_id != compactions.conversation_id)"
        )
    )
    if has_context:
        raise RuntimeError(
            "Cannot remove compaction membership while context or inherited history is recorded."
        )
    with op.batch_alter_table("compactions", schema=None, recreate="never") as batch_op:
        batch_op.add_column(sa.Column("native_id", sa.VARCHAR(), nullable=True))
        batch_op.drop_column("membership_complete")

    with op.batch_alter_table("compaction_memberships", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_compaction_memberships_model_message_id"))

    op.drop_table("compaction_memberships")
    with op.batch_alter_table("conversation_compactions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_conversation_compactions_compaction_id"))

    op.drop_table("conversation_compactions")
