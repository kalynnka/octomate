"""add deferred action batches and actions

Revision ID: 35a8dfbb3d1a
Revises: 9c72d4a33a10
Create Date: 2026-05-27 02:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import octomate.models.messages  # noqa: F401 — referenced by PydanticJSON columns


# revision identifiers, used by Alembic.
revision: str = "35a8dfbb3d1a"
down_revision: Union[str, Sequence[str], None] = "9c72d4a33a10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "deferred_action_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("agent_tentacle_id", sa.String(), nullable=False),
        sa.Column("run_name", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "source_key",
            octomate.models.messages.PydanticJSON(),
            nullable=False,
        ),
        sa.Column(
            "target_key",
            octomate.models.messages.PydanticJSON(),
            nullable=False,
        ),
        sa.Column("target_mode", sa.String(), nullable=False),
        sa.Column(
            "decision",
            octomate.models.messages.PydanticJSON(),
            nullable=True,
        ),
        sa.Column(
            "requests",
            octomate.models.messages.PydanticJSON(),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_deferred_action_batches_agent_tentacle_id"),
        "deferred_action_batches",
        ["agent_tentacle_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deferred_action_batches_completed_at"),
        "deferred_action_batches",
        ["completed_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deferred_action_batches_conversation_id"),
        "deferred_action_batches",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deferred_action_batches_created_at"),
        "deferred_action_batches",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deferred_action_batches_run_name"),
        "deferred_action_batches",
        ["run_name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deferred_action_batches_updated_at"),
        "deferred_action_batches",
        ["updated_at"],
        unique=False,
    )

    op.create_table(
        "deferred_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("tool_call_id", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("args", octomate.models.messages.PydanticJSON(), nullable=False),
        sa.Column(
            "metadata",
            octomate.models.messages.PydanticJSON(),
            nullable=False,
        ),
        sa.Column("result", octomate.models.messages.PydanticJSON(), nullable=True),
        sa.Column("platform_message_id", sa.String(), nullable=True),
        sa.Column("responder_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["deferred_action_batches.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_deferred_actions_batch_id"),
        "deferred_actions",
        ["batch_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deferred_actions_created_at"),
        "deferred_actions",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deferred_actions_kind"),
        "deferred_actions",
        ["kind"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deferred_actions_resolved_at"),
        "deferred_actions",
        ["resolved_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deferred_actions_responder_id"),
        "deferred_actions",
        ["responder_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deferred_actions_tool_call_id"),
        "deferred_actions",
        ["tool_call_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deferred_actions_tool_name"),
        "deferred_actions",
        ["tool_name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deferred_actions_updated_at"),
        "deferred_actions",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_deferred_actions_updated_at"), table_name="deferred_actions")
    op.drop_index(op.f("ix_deferred_actions_tool_name"), table_name="deferred_actions")
    op.drop_index(
        op.f("ix_deferred_actions_tool_call_id"),
        table_name="deferred_actions",
    )
    op.drop_index(
        op.f("ix_deferred_actions_responder_id"),
        table_name="deferred_actions",
    )
    op.drop_index(
        op.f("ix_deferred_actions_resolved_at"), table_name="deferred_actions"
    )
    op.drop_index(op.f("ix_deferred_actions_kind"), table_name="deferred_actions")
    op.drop_index(op.f("ix_deferred_actions_created_at"), table_name="deferred_actions")
    op.drop_index(op.f("ix_deferred_actions_batch_id"), table_name="deferred_actions")
    op.drop_table("deferred_actions")

    op.drop_index(
        op.f("ix_deferred_action_batches_updated_at"),
        table_name="deferred_action_batches",
    )
    op.drop_index(
        op.f("ix_deferred_action_batches_run_name"),
        table_name="deferred_action_batches",
    )
    op.drop_index(
        op.f("ix_deferred_action_batches_created_at"),
        table_name="deferred_action_batches",
    )
    op.drop_index(
        op.f("ix_deferred_action_batches_conversation_id"),
        table_name="deferred_action_batches",
    )
    op.drop_index(
        op.f("ix_deferred_action_batches_completed_at"),
        table_name="deferred_action_batches",
    )
    op.drop_index(
        op.f("ix_deferred_action_batches_agent_tentacle_id"),
        table_name="deferred_action_batches",
    )
    op.drop_table("deferred_action_batches")
