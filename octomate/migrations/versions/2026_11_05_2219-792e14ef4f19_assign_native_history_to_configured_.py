"""Assign native history to default runtime agents without changing its provenance.

Ownership, thread addresses, sender profiles, deferred-action addresses, and run
names use the runtime agents. Session/turn IDs and the persisted native thread kind
remain unchanged. The redundant run runtime namespace and its index are removed.
Conversation, history, and uploaded-file references keep their existing ids;
conflicting owners or addresses abort the migration without merging history.

Revision ID: 792e14ef4f19
Revises: 1a83da0aa445
Create Date: 2026-11-05 22:19:40.105461

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "792e14ef4f19"
down_revision: str | Sequence[str] | None = "1a83da0aa445"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rewrite stored ownership once, so application code needs no aliases."""
    bind = op.get_bind()
    owners = {
        "codex-native": "codex",
        "claude-native": "claude",
        "deepseek-native": "deepseek",
    }
    references = (
        ("conversations", "agent_tentacle_id"),
        ("thread_messages", "agent_tentacle_id"),
        ("channel_handoffs", "source_agent_tentacle_id"),
        ("channel_handoffs", "to_agent_tentacle_id"),
        ("deferred_action_batches", "agent_tentacle_id"),
        ("threads", "channel_tentacle_id"),
        ("user_profiles", "channel_tentacle_id"),
        ("agent_runs", "name"),
    )
    tables = [
        sa.table(name, sa.column(column, sa.String())) for name, column in references
    ]
    batches = sa.table(
        "deferred_action_batches",
        sa.column("source_address", sa.JSON()),
        sa.column("target_address", sa.JSON()),
    )
    conversations = sa.table(
        "conversations",
        sa.column("thread_id", sa.Uuid()),
        sa.column("agent_tentacle_id", sa.String()),
        sa.column("subagent_id", sa.String()),
    )
    existing = conversations.alias("existing")
    for native_id, owner in owners.items():
        conflict = bind.execute(
            sa.select(conversations.c.thread_id)
            .select_from(
                conversations.join(
                    existing,
                    sa.and_(
                        conversations.c.thread_id == existing.c.thread_id,
                        conversations.c.subagent_id == existing.c.subagent_id,
                        existing.c.agent_tentacle_id == owner,
                    ),
                )
            )
            .where(conversations.c.agent_tentacle_id == native_id)
            .limit(1)
        ).first()
        if conflict is not None:
            raise RuntimeError(
                f"Cannot assign {native_id} to {owner}: a conversation already owns that context"
            )

    with bind.begin_nested():
        for table, (_, column) in zip(tables, references, strict=True):
            for native_id, owner in owners.items():
                bind.execute(
                    table.update()
                    .where(table.c[column] == native_id)
                    .values({column: owner})
                )
        for column in ("source_address", "target_address"):
            for native_id, owner in owners.items():
                bind.execute(
                    batches.update()
                    .where(
                        sa.func.json_extract(batches.c[column], "$.channel_tentacle_id")
                        == native_id
                    )
                    .values(
                        {
                            column: sa.func.json_set(
                                batches.c[column], "$.channel_tentacle_id", owner
                            )
                        }
                    )
                )
        # Rebuilding this table would disturb messages and fork histories referencing it.
        with op.batch_alter_table(
            "agent_runs", schema=None, recreate="never"
        ) as batch_op:
            batch_op.drop_index(batch_op.f("ix_agent_runs_native_id"))
            batch_op.drop_column("native_id")


def downgrade() -> None:
    """Original owners and runtime namespaces cannot be inferred from configured ids."""
    raise RuntimeError(
        "Restore a database backup to undo the native ownership migration"
    )
