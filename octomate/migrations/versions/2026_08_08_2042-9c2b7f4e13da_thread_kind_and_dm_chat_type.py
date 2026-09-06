"""thread kind and the three chat surfaces

`ChatType` is `dm | group | thread` — the three surfaces a channel offers, where it
used to be `private | group` with a thread hidden in a non-empty `thread_id`. Stored
values move with it: `threads.chat_type`, and the address each deferred batch
serializes, which would otherwise stop validating.

`threads.kind` then records the surface on the row: the chat type, or `native_thread`
for a Claude/Codex session's own thread. Only a `thread` and a `native_thread` may
carry a `project_id`; a DM or a group chat outlives every project in it and the
binding is frozen.

`threads.thread_id` becomes `channel_thread_id`, since `thread_id` on every other
table is a foreign key to `threads.id` and the two were read as one thing. It is
nullable now: off a thread there is no platform thread, and `""` only ever stood in
because it doubled as the "is this a thread" flag `chat_type` now carries.

That splits `uq_threads_key` in two, because NULL never equals NULL and a single
four-column constraint would stop refusing duplicate DMs the moment the column went
nullable: `uq_threads_thread` is unique per thread, `uq_threads_chat` unique per chat
that is not one.

Existing rows relabel by the same rule the code now applies — a native tentacle or a
non-empty `thread_id` is a thread, and a whole chat keeps its word. `uq_threads_key`
holds `chat_type`, but no two rows can collide: the mapping is injective per row.

The downgrade cannot tell which chat a `thread` row was in, since that is what this
migration folds away, so it sends every one back to `private`. That is exact for rows
written before this (every one is a native session or a Slack thread in an IM) and
lossy for a thread opened in a group chat afterwards.

Revision ID: 9c2b7f4e13da
Revises: c4e2a91d0f38
Create Date: 2026-08-08 20:42:44.913277

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c2b7f4e13da"
down_revision: str | Sequence[str] | None = "c4e2a91d0f38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KIND_COMMENT = (
    "Which surface this thread is: dm, group, thread, or native_thread. Written when "
    "the row is created and never changed. Only a thread and a native_thread may "
    "carry a project_id."
)

NATIVE = "channel_tentacle_id IN ('claude-native', 'codex-native')"

BATCH_ADDRESS = """
    UPDATE deferred_action_batches
    SET {column} = json_set({column}, '$.chat_type', '{new}')
    WHERE json_extract({column}, '$.chat_type') = '{old}'
"""


def relabel_batch_addresses(old: str, new: str) -> None:
    for column in ("source_address", "target_address"):
        op.execute(BATCH_ADDRESS.format(column=column, old=old, new=new))


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index("ix_threads_thread_id", table_name="threads")
    with op.batch_alter_table("threads", schema=None) as batch_op:
        batch_op.alter_column("thread_id", new_column_name="channel_thread_id")
    op.create_index("ix_threads_channel_thread_id", "threads", ["channel_thread_id"])
    op.execute(
        f"""
        UPDATE threads SET chat_type = CASE
            WHEN {NATIVE} OR channel_thread_id != '' THEN 'thread'
            WHEN chat_type = 'private' THEN 'dm'
            ELSE chat_type
        END
        """
    )
    relabel_batch_addresses("private", "dm")

    with op.batch_alter_table("threads", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("kind", sa.String(), nullable=True, comment=KIND_COMMENT)
        )
    op.execute(
        f"UPDATE threads SET kind = CASE WHEN {NATIVE} THEN 'native_thread' ELSE chat_type END"
    )
    with op.batch_alter_table("threads", schema=None) as batch_op:
        batch_op.alter_column(
            "kind",
            existing_type=sa.String(),
            existing_comment=KIND_COMMENT,
            nullable=False,
        )
        batch_op.create_index(batch_op.f("ix_threads_kind"), ["kind"], unique=False)

    with op.batch_alter_table("threads", schema=None) as batch_op:
        batch_op.drop_constraint("uq_threads_key", type_="unique")
        batch_op.alter_column(
            "channel_thread_id", existing_type=sa.String(), nullable=True
        )
    op.execute(
        "UPDATE threads SET channel_thread_id = NULL WHERE channel_thread_id = ''"
    )
    op.create_index(
        "uq_threads_thread",
        "threads",
        ["channel_tentacle_id", "chat_type", "chat_id", "channel_thread_id"],
        unique=True,
        sqlite_where=sa.text("channel_thread_id IS NOT NULL"),
    )
    op.create_index(
        "uq_threads_chat",
        "threads",
        ["channel_tentacle_id", "chat_type", "chat_id"],
        unique=True,
        sqlite_where=sa.text("channel_thread_id IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_threads_chat", table_name="threads")
    op.drop_index("uq_threads_thread", table_name="threads")
    op.execute(
        "UPDATE threads SET channel_thread_id = '' WHERE channel_thread_id IS NULL"
    )
    with op.batch_alter_table("threads", schema=None) as batch_op:
        batch_op.alter_column(
            "channel_thread_id",
            existing_type=sa.String(),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_threads_key",
            ["channel_tentacle_id", "chat_type", "chat_id", "channel_thread_id"],
        )
    with op.batch_alter_table("threads", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_threads_kind"))
        batch_op.drop_column("kind")
    op.execute(
        "UPDATE threads SET chat_type = 'private' WHERE chat_type IN ('dm', 'thread')"
    )
    relabel_batch_addresses("dm", "private")
    op.drop_index("ix_threads_channel_thread_id", table_name="threads")
    with op.batch_alter_table("threads", schema=None) as batch_op:
        batch_op.alter_column("channel_thread_id", new_column_name="thread_id")
    op.create_index("ix_threads_thread_id", "threads", ["thread_id"])
