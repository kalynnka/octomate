"""channel thread history foundation

Revision ID: b4c5d6e7f8a9
Revises: a1b2c3d4e5f6
Create Date: 2026-06-24 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import octomate.models.messages  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "b4c5d6e7f8a9"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "channel_threads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_tentacle_id", sa.String(), nullable=False),
        sa.Column("chat_type", sa.String(), nullable=False),
        sa.Column("chat_id", sa.String(), nullable=False),
        sa.Column("thread_id", sa.String(), nullable=False),
        sa.Column("prompt_cursor_message_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel_tentacle_id",
            "chat_type",
            "chat_id",
            "thread_id",
            name="uq_channel_threads_key",
        ),
    )
    op.create_index(
        op.f("ix_channel_threads_channel_tentacle_id"),
        "channel_threads",
        ["channel_tentacle_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_threads_chat_id"),
        "channel_threads",
        ["chat_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_threads_chat_type"),
        "channel_threads",
        ["chat_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_threads_created_at"),
        "channel_threads",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_threads_prompt_cursor_message_id"),
        "channel_threads",
        ["prompt_cursor_message_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_threads_status"),
        "channel_threads",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_threads_thread_id"),
        "channel_threads",
        ["thread_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_threads_updated_at"),
        "channel_threads",
        ["updated_at"],
        unique=False,
    )

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("channel_thread_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_conversations_channel_thread_id_channel_threads",
            "channel_threads",
            ["channel_thread_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            op.f("ix_conversations_channel_thread_id"),
            ["channel_thread_id"],
            unique=False,
        )

    op.create_table(
        "channel_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_thread_id", sa.Uuid(), nullable=False),
        sa.Column("platform_message_id", sa.String(), nullable=True),
        sa.Column("reply_id", sa.String(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("actor_kind", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("agent_tentacle_id", sa.String(), nullable=True),
        sa.Column(
            "sender",
            octomate.models.messages.PydanticJSON(),
            nullable=False,
        ),
        sa.Column(
            "segments",
            octomate.models.messages.PydanticJSON(),
            nullable=False,
        ),
        sa.Column("message_text", sa.String(), nullable=True),
        sa.Column("raw", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_thread_id"],
            ["channel_threads.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_channel_messages_actor_kind"),
        "channel_messages",
        ["actor_kind"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_messages_agent_tentacle_id"),
        "channel_messages",
        ["agent_tentacle_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_messages_channel_thread_id"),
        "channel_messages",
        ["channel_thread_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_messages_created_at"),
        "channel_messages",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_messages_direction"),
        "channel_messages",
        ["direction"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_messages_message_text"),
        "channel_messages",
        ["message_text"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_messages_platform_message_id"),
        "channel_messages",
        ["platform_message_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_messages_reply_id"),
        "channel_messages",
        ["reply_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_messages_timestamp"),
        "channel_messages",
        ["timestamp"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_messages_user_id"),
        "channel_messages",
        ["user_id"],
        unique=False,
    )

    with op.batch_alter_table("channel_threads") as batch_op:
        batch_op.create_foreign_key(
            "fk_channel_threads_prompt_cursor_message_id_channel_messages",
            "channel_messages",
            ["prompt_cursor_message_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_table(
        "channel_handoffs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_thread_id", sa.Uuid(), nullable=False),
        sa.Column("from_agent_tentacle_id", sa.String(), nullable=True),
        sa.Column("to_agent_tentacle_id", sa.String(), nullable=False),
        sa.Column("to_model", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("hint", sa.String(), nullable=False),
        sa.Column("brief", sa.String(), nullable=False),
        sa.Column("source_conversation_id", sa.Uuid(), nullable=True),
        sa.Column("target_conversation_id", sa.Uuid(), nullable=True),
        sa.Column("source_run_id", sa.String(), nullable=True),
        sa.Column("source_model_message_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_thread_id"],
            ["channel_threads.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_conversation_id"],
            ["conversations.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_model_message_id"],
            ["model_messages.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_run_id"],
            ["agent_runs.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_conversation_id"],
            ["conversations.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_channel_handoffs_channel_thread_id"),
        "channel_handoffs",
        ["channel_thread_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_handoffs_created_at"),
        "channel_handoffs",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_handoffs_from_agent_tentacle_id"),
        "channel_handoffs",
        ["from_agent_tentacle_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_handoffs_source_conversation_id"),
        "channel_handoffs",
        ["source_conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_handoffs_source_model_message_id"),
        "channel_handoffs",
        ["source_model_message_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_handoffs_source_run_id"),
        "channel_handoffs",
        ["source_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_handoffs_target_conversation_id"),
        "channel_handoffs",
        ["target_conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_handoffs_to_agent_tentacle_id"),
        "channel_handoffs",
        ["to_agent_tentacle_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channel_handoffs_to_model"),
        "channel_handoffs",
        ["to_model"],
        unique=False,
    )

    op.create_table(
        "message_binding",
        sa.Column("channel_message_id", sa.Uuid(), nullable=False),
        sa.Column("model_message_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("tool_call_id", sa.String(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_message_id"],
            ["channel_messages.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_message_id"],
            ["model_messages.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("channel_message_id", "model_message_id", "kind"),
    )
    op.create_index(
        op.f("ix_message_binding_created_at"),
        "message_binding",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_message_binding_run_id"),
        "message_binding",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_message_binding_tool_call_id"),
        "message_binding",
        ["tool_call_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_message_binding_tool_call_id"),
        table_name="message_binding",
    )
    op.drop_index(
        op.f("ix_message_binding_run_id"),
        table_name="message_binding",
    )
    op.drop_index(
        op.f("ix_message_binding_created_at"),
        table_name="message_binding",
    )
    op.drop_table("message_binding")

    op.drop_index(op.f("ix_channel_handoffs_to_model"), table_name="channel_handoffs")
    op.drop_index(
        op.f("ix_channel_handoffs_to_agent_tentacle_id"),
        table_name="channel_handoffs",
    )
    op.drop_index(
        op.f("ix_channel_handoffs_target_conversation_id"),
        table_name="channel_handoffs",
    )
    op.drop_index(
        op.f("ix_channel_handoffs_source_run_id"),
        table_name="channel_handoffs",
    )
    op.drop_index(
        op.f("ix_channel_handoffs_source_model_message_id"),
        table_name="channel_handoffs",
    )
    op.drop_index(
        op.f("ix_channel_handoffs_source_conversation_id"),
        table_name="channel_handoffs",
    )
    op.drop_index(
        op.f("ix_channel_handoffs_from_agent_tentacle_id"),
        table_name="channel_handoffs",
    )
    op.drop_index(
        op.f("ix_channel_handoffs_created_at"),
        table_name="channel_handoffs",
    )
    op.drop_index(
        op.f("ix_channel_handoffs_channel_thread_id"),
        table_name="channel_handoffs",
    )
    op.drop_table("channel_handoffs")

    with op.batch_alter_table("channel_threads") as batch_op:
        batch_op.drop_constraint(
            "fk_channel_threads_prompt_cursor_message_id_channel_messages",
            type_="foreignkey",
        )
    op.drop_index(op.f("ix_channel_messages_user_id"), table_name="channel_messages")
    op.drop_index(op.f("ix_channel_messages_timestamp"), table_name="channel_messages")
    op.drop_index(op.f("ix_channel_messages_reply_id"), table_name="channel_messages")
    op.drop_index(
        op.f("ix_channel_messages_platform_message_id"),
        table_name="channel_messages",
    )
    op.drop_index(
        op.f("ix_channel_messages_message_text"),
        table_name="channel_messages",
    )
    op.drop_index(op.f("ix_channel_messages_direction"), table_name="channel_messages")
    op.drop_index(op.f("ix_channel_messages_created_at"), table_name="channel_messages")
    op.drop_index(
        op.f("ix_channel_messages_channel_thread_id"),
        table_name="channel_messages",
    )
    op.drop_index(
        op.f("ix_channel_messages_agent_tentacle_id"),
        table_name="channel_messages",
    )
    op.drop_index(op.f("ix_channel_messages_actor_kind"), table_name="channel_messages")
    op.drop_table("channel_messages")

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_index(op.f("ix_conversations_channel_thread_id"))
        batch_op.drop_constraint(
            "fk_conversations_channel_thread_id_channel_threads",
            type_="foreignkey",
        )
        batch_op.drop_column("channel_thread_id")

    op.drop_index(op.f("ix_channel_threads_updated_at"), table_name="channel_threads")
    op.drop_index(op.f("ix_channel_threads_thread_id"), table_name="channel_threads")
    op.drop_index(op.f("ix_channel_threads_status"), table_name="channel_threads")
    op.drop_index(
        op.f("ix_channel_threads_prompt_cursor_message_id"),
        table_name="channel_threads",
    )
    op.drop_index(op.f("ix_channel_threads_created_at"), table_name="channel_threads")
    op.drop_index(op.f("ix_channel_threads_chat_type"), table_name="channel_threads")
    op.drop_index(op.f("ix_channel_threads_chat_id"), table_name="channel_threads")
    op.drop_index(
        op.f("ix_channel_threads_channel_tentacle_id"),
        table_name="channel_threads",
    )
    op.drop_table("channel_threads")
