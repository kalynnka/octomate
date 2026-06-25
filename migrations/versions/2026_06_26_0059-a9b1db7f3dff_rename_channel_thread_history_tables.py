"""rename channel thread history tables

Revision ID: a9b1db7f3dff
Revises: b4c5d6e7f8a9
Create Date: 2026-06-26 00:59:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a9b1db7f3dff"
down_revision: Union[str, Sequence[str], None] = "b4c5d6e7f8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index(
        op.f("ix_channel_messages_actor_kind"),
        table_name="channel_messages",
    )
    op.drop_index(
        op.f("ix_channel_messages_agent_tentacle_id"),
        table_name="channel_messages",
    )
    op.drop_index(
        op.f("ix_channel_messages_channel_thread_id"),
        table_name="channel_messages",
    )
    op.drop_index(
        op.f("ix_channel_messages_created_at"),
        table_name="channel_messages",
    )
    op.drop_index(
        op.f("ix_channel_messages_direction"),
        table_name="channel_messages",
    )
    op.drop_index(
        op.f("ix_channel_messages_message_text"),
        table_name="channel_messages",
    )
    op.drop_index(
        op.f("ix_channel_messages_platform_message_id"),
        table_name="channel_messages",
    )
    op.drop_index(
        op.f("ix_channel_messages_reply_id"),
        table_name="channel_messages",
    )
    op.drop_index(
        op.f("ix_channel_messages_timestamp"),
        table_name="channel_messages",
    )
    op.drop_index(
        op.f("ix_channel_messages_user_id"),
        table_name="channel_messages",
    )

    op.drop_index(
        op.f("ix_channel_threads_channel_tentacle_id"),
        table_name="channel_threads",
    )
    op.drop_index(op.f("ix_channel_threads_chat_id"), table_name="channel_threads")
    op.drop_index(
        op.f("ix_channel_threads_chat_type"),
        table_name="channel_threads",
    )
    op.drop_index(
        op.f("ix_channel_threads_created_at"),
        table_name="channel_threads",
    )
    op.drop_index(
        op.f("ix_channel_threads_prompt_cursor_message_id"),
        table_name="channel_threads",
    )
    op.drop_index(op.f("ix_channel_threads_status"), table_name="channel_threads")
    op.drop_index(
        op.f("ix_channel_threads_thread_id"),
        table_name="channel_threads",
    )
    op.drop_index(
        op.f("ix_channel_threads_updated_at"),
        table_name="channel_threads",
    )

    op.drop_index(
        op.f("ix_channel_handoffs_channel_thread_id"),
        table_name="channel_handoffs",
    )

    with op.batch_alter_table("channel_threads") as batch_op:
        batch_op.drop_constraint(
            "fk_channel_threads_prompt_cursor_message_id_channel_messages",
            type_="foreignkey",
        )
        batch_op.drop_constraint("uq_channel_threads_key", type_="unique")

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_index(op.f("ix_conversations_channel_thread_id"))
        batch_op.drop_index(op.f("ix_conversations_thread_id"))
        batch_op.drop_constraint(
            "fk_conversations_channel_thread_id_channel_threads",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "uq_conversations_conversation_key",
            type_="unique",
        )

    op.rename_table("channel_threads", "threads")
    op.rename_table("channel_messages", "thread_messages")

    with op.batch_alter_table("thread_messages") as batch_op:
        batch_op.alter_column(
            "channel_thread_id",
            new_column_name="thread_id",
            existing_type=sa.Uuid(),
            existing_nullable=False,
        )

    with op.batch_alter_table("channel_handoffs") as batch_op:
        batch_op.alter_column(
            "channel_thread_id",
            new_column_name="thread_id",
            existing_type=sa.Uuid(),
            existing_nullable=False,
        )

    with op.batch_alter_table("message_binding") as batch_op:
        batch_op.alter_column(
            "channel_message_id",
            new_column_name="thread_message_id",
            existing_type=sa.Uuid(),
            existing_nullable=False,
        )

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.alter_column(
            "channel_thread_id",
            new_column_name="octomate_thread_id",
            existing_type=sa.Uuid(),
            existing_nullable=True,
        )

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.alter_column(
            "thread_id",
            new_column_name="channel_thread_id",
            existing_type=sa.String(),
            existing_nullable=False,
        )

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.alter_column(
            "octomate_thread_id",
            new_column_name="thread_id",
            existing_type=sa.Uuid(),
            existing_nullable=True,
        )

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.create_index(
            op.f("ix_conversations_channel_thread_id"),
            ["channel_thread_id"],
            unique=False,
        )
        batch_op.create_index(
            op.f("ix_conversations_thread_id"),
            ["thread_id"],
            unique=False,
        )
        batch_op.create_unique_constraint(
            "uq_conversations_conversation_key",
            [
                "channel_tentacle_id",
                "chat_type",
                "chat_id",
                "channel_thread_id",
                "user_id",
                "agent_tentacle_id",
            ],
        )
        batch_op.create_foreign_key(
            "fk_conversations_thread_id_threads",
            "threads",
            ["thread_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("threads") as batch_op:
        batch_op.create_unique_constraint(
            "uq_threads_key",
            ["channel_tentacle_id", "chat_type", "chat_id", "thread_id"],
        )
        batch_op.create_foreign_key(
            "fk_threads_prompt_cursor_message_id_thread_messages",
            "thread_messages",
            ["prompt_cursor_message_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_index(
        op.f("ix_threads_channel_tentacle_id"),
        "threads",
        ["channel_tentacle_id"],
        unique=False,
    )
    op.create_index(op.f("ix_threads_chat_id"), "threads", ["chat_id"], unique=False)
    op.create_index(
        op.f("ix_threads_chat_type"),
        "threads",
        ["chat_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_threads_created_at"),
        "threads",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_threads_prompt_cursor_message_id"),
        "threads",
        ["prompt_cursor_message_id"],
        unique=False,
    )
    op.create_index(op.f("ix_threads_status"), "threads", ["status"], unique=False)
    op.create_index(
        op.f("ix_threads_thread_id"),
        "threads",
        ["thread_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_threads_updated_at"),
        "threads",
        ["updated_at"],
        unique=False,
    )

    op.create_index(
        op.f("ix_thread_messages_actor_kind"),
        "thread_messages",
        ["actor_kind"],
        unique=False,
    )
    op.create_index(
        op.f("ix_thread_messages_agent_tentacle_id"),
        "thread_messages",
        ["agent_tentacle_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_thread_messages_thread_id"),
        "thread_messages",
        ["thread_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_thread_messages_created_at"),
        "thread_messages",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_thread_messages_direction"),
        "thread_messages",
        ["direction"],
        unique=False,
    )
    op.create_index(
        op.f("ix_thread_messages_message_text"),
        "thread_messages",
        ["message_text"],
        unique=False,
    )
    op.create_index(
        op.f("ix_thread_messages_platform_message_id"),
        "thread_messages",
        ["platform_message_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_thread_messages_reply_id"),
        "thread_messages",
        ["reply_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_thread_messages_timestamp"),
        "thread_messages",
        ["timestamp"],
        unique=False,
    )
    op.create_index(
        op.f("ix_thread_messages_user_id"),
        "thread_messages",
        ["user_id"],
        unique=False,
    )

    op.create_index(
        op.f("ix_channel_handoffs_thread_id"),
        "channel_handoffs",
        ["thread_id"],
        unique=False,
    )

    with op.batch_alter_table("deferred_action_batches") as batch_op:
        batch_op.alter_column(
            "source_key",
            new_column_name="source_address",
            existing_type=sa.JSON(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "target_key",
            new_column_name="target_address",
            existing_type=sa.JSON(),
            existing_nullable=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("deferred_action_batches") as batch_op:
        batch_op.alter_column(
            "source_address",
            new_column_name="source_key",
            existing_type=sa.JSON(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "target_address",
            new_column_name="target_key",
            existing_type=sa.JSON(),
            existing_nullable=False,
        )

    op.drop_index(
        op.f("ix_channel_handoffs_thread_id"),
        table_name="channel_handoffs",
    )

    op.drop_index(op.f("ix_thread_messages_user_id"), table_name="thread_messages")
    op.drop_index(op.f("ix_thread_messages_timestamp"), table_name="thread_messages")
    op.drop_index(op.f("ix_thread_messages_reply_id"), table_name="thread_messages")
    op.drop_index(
        op.f("ix_thread_messages_platform_message_id"),
        table_name="thread_messages",
    )
    op.drop_index(
        op.f("ix_thread_messages_message_text"),
        table_name="thread_messages",
    )
    op.drop_index(op.f("ix_thread_messages_direction"), table_name="thread_messages")
    op.drop_index(op.f("ix_thread_messages_created_at"), table_name="thread_messages")
    op.drop_index(op.f("ix_thread_messages_thread_id"), table_name="thread_messages")
    op.drop_index(
        op.f("ix_thread_messages_agent_tentacle_id"),
        table_name="thread_messages",
    )
    op.drop_index(op.f("ix_thread_messages_actor_kind"), table_name="thread_messages")

    op.drop_index(op.f("ix_threads_updated_at"), table_name="threads")
    op.drop_index(op.f("ix_threads_thread_id"), table_name="threads")
    op.drop_index(op.f("ix_threads_status"), table_name="threads")
    op.drop_index(op.f("ix_threads_prompt_cursor_message_id"), table_name="threads")
    op.drop_index(op.f("ix_threads_created_at"), table_name="threads")
    op.drop_index(op.f("ix_threads_chat_type"), table_name="threads")
    op.drop_index(op.f("ix_threads_chat_id"), table_name="threads")
    op.drop_index(op.f("ix_threads_channel_tentacle_id"), table_name="threads")

    with op.batch_alter_table("threads") as batch_op:
        batch_op.drop_constraint(
            "fk_threads_prompt_cursor_message_id_thread_messages",
            type_="foreignkey",
        )
        batch_op.drop_constraint("uq_threads_key", type_="unique")

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_index(op.f("ix_conversations_thread_id"))
        batch_op.drop_index(op.f("ix_conversations_channel_thread_id"))
        batch_op.drop_constraint(
            "fk_conversations_thread_id_threads",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "uq_conversations_conversation_key",
            type_="unique",
        )
        batch_op.alter_column(
            "thread_id",
            new_column_name="octomate_thread_id",
            existing_type=sa.Uuid(),
            existing_nullable=True,
        )

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.alter_column(
            "channel_thread_id",
            new_column_name="thread_id",
            existing_type=sa.String(),
            existing_nullable=False,
        )

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.alter_column(
            "octomate_thread_id",
            new_column_name="channel_thread_id",
            existing_type=sa.Uuid(),
            existing_nullable=True,
        )

    with op.batch_alter_table("message_binding") as batch_op:
        batch_op.alter_column(
            "thread_message_id",
            new_column_name="channel_message_id",
            existing_type=sa.Uuid(),
            existing_nullable=False,
        )

    with op.batch_alter_table("channel_handoffs") as batch_op:
        batch_op.alter_column(
            "thread_id",
            new_column_name="channel_thread_id",
            existing_type=sa.Uuid(),
            existing_nullable=False,
        )

    with op.batch_alter_table("thread_messages") as batch_op:
        batch_op.alter_column(
            "thread_id",
            new_column_name="channel_thread_id",
            existing_type=sa.Uuid(),
            existing_nullable=False,
        )

    op.rename_table("thread_messages", "channel_messages")
    op.rename_table("threads", "channel_threads")

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.create_index(
            op.f("ix_conversations_channel_thread_id"),
            ["channel_thread_id"],
            unique=False,
        )
        batch_op.create_index(
            op.f("ix_conversations_thread_id"),
            ["thread_id"],
            unique=False,
        )
        batch_op.create_unique_constraint(
            "uq_conversations_conversation_key",
            [
                "channel_tentacle_id",
                "chat_type",
                "chat_id",
                "thread_id",
                "user_id",
                "agent_tentacle_id",
            ],
        )
        batch_op.create_foreign_key(
            "fk_conversations_channel_thread_id_channel_threads",
            "channel_threads",
            ["channel_thread_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("channel_threads") as batch_op:
        batch_op.create_unique_constraint(
            "uq_channel_threads_key",
            ["channel_tentacle_id", "chat_type", "chat_id", "thread_id"],
        )
        batch_op.create_foreign_key(
            "fk_channel_threads_prompt_cursor_message_id_channel_messages",
            "channel_messages",
            ["prompt_cursor_message_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_index(
        op.f("ix_channel_handoffs_channel_thread_id"),
        "channel_handoffs",
        ["channel_thread_id"],
        unique=False,
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
