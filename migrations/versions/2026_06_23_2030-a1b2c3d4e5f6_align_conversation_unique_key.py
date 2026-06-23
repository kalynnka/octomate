"""align conversation unique key

Revision ID: a1b2c3d4e5f6
Revises: e3f4a5b6c7d8
Create Date: 2026-06-23 20:30:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "e3f4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_constraint(
            "uq_conversations_conversation_key",
            type_="unique",
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


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_constraint(
            "uq_conversations_conversation_key",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_conversations_conversation_key",
            [
                "chat_type",
                "chat_id",
                "thread_id",
                "user_id",
            ],
        )
