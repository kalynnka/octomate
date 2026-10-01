"""Store deferred handoff addresses directly.

Convert saved landing variants into a neutral address and a separate creation
flag, so deferred handoffs resume without a runtime compatibility adapter.
Downgrade refuses address reuse, which the old variants cannot represent.

Revision ID: a623321f464c
Revises: decb63376d53
Create Date: 2026-10-02 06:23:15.703489

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a623321f464c"
down_revision: str | Sequence[str] | None = "decb63376d53"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Replace landing wrappers in saved handoffs."""
    op.execute(
        """
        UPDATE deferred_action_batches
        SET decision = json_set(
            decision,
            '$.new_thread', json(CASE
                WHEN json_extract(decision, '$.destination.kind') = 'here'
                THEN 'false' ELSE 'true' END),
            '$.destination', json(CASE
                WHEN json_extract(decision, '$.destination.kind') = 'crossing'
                THEN json_extract(decision, '$.destination.address')
                ELSE 'null' END)
        )
        WHERE json_extract(decision, '$.destination.kind') IN ('here', 'thread')
            OR (
                json_extract(decision, '$.destination.kind') = 'crossing'
                AND json_type(decision, '$.destination.address') = 'object'
            )
        """
    )


def downgrade() -> None:
    """Restore landing wrappers without discarding an explicit address."""
    reuses_address = op.get_bind().scalar(
        sa.text(
            """
            SELECT 1 FROM deferred_action_batches
            WHERE json_type(decision, '$.destination') = 'object'
                AND json_type(decision, '$.new_thread') = 'false'
            LIMIT 1
            """
        )
    )
    if reuses_address:
        raise RuntimeError(
            "Cannot downgrade deferred handoffs that reuse an explicit address."
        )
    op.execute(
        """
        UPDATE deferred_action_batches
        SET decision = json_remove(json_set(
            decision,
            '$.destination', json(CASE
                WHEN json_type(decision, '$.new_thread') = 'false'
                THEN json_object('kind', 'here')
                WHEN json_type(decision, '$.destination') = 'null'
                THEN json_object('kind', 'thread')
                ELSE json_object('kind', 'crossing',
                    'address', json_extract(decision, '$.destination')) END)
        ), '$.new_thread')
        WHERE json_type(decision, '$.new_thread') IN ('true', 'false')
            AND (
                json_type(decision, '$.destination') = 'null'
                OR json_type(decision, '$.destination.channel_tentacle_id') = 'text'
            )
        """
    )
