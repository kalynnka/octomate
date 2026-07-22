"""user identity registry

Two tables for cross-channel identity: `users` (one row per human — `handle`
is the `users:` config key) and `user_profiles` (one row per (channel,
platform user) ever seen — the persisted per-channel profile). The link
between them is not a table: it is `user_profiles.user_id`, a nullable FK
set only when proven (method: config | code), NULL for a profile that is
merely an observation.

The platform string (Slack Uxxx, Lark open_id, QQ number) lives in
`channel_user_id`; legacy wire payloads carry it as `user_id`, which the
schema's before-validator migrates so it never lands in the FK.

`thread_messages.sender` normalizes into the registry: the JSON snapshot
column becomes `sender_id`, an FK to the sender's `user_profiles` row. The
backfill parses every historical sender blob (legacy shape: the platform id
under `user_id`), upserting one profile per (channel, platform user) with the
most recent snapshot winning, then drops the blob column. Historical outbound
rows carried fabricated senders (the agent tentacle id as a platform id);
those become plain unlinked observation rows.

The FK's ON DELETE SET NULL is documentation on SQLite (FK enforcement is off
in this deployment); UserManager NULLs a deleted user's links explicitly.

Revision ID: a91c04d5e7f2
Revises: 417719624acf
Create Date: 2026-07-22 14:00:00.000000

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from uuid_utils.compat import uuid7


# revision identifiers, used by Alembic.
revision: str = 'a91c04d5e7f2'
down_revision: Union[str, Sequence[str], None] = '417719624acf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'users',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('handle', sa.String(), nullable=False, comment='Stable slug naming this human — the `users:` config key.'),
        sa.Column('name', sa.String(), nullable=False, comment='Canonical display name the agent uses for this human.'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('handle', name=op.f('uq_users_handle')),
    )
    op.create_table(
        'user_profiles',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('channel_tentacle_id', sa.String(), nullable=False, comment="The channel this identity lives on; with channel_user_id it is the profile's identity — one row per (channel, platform user) ever seen."),
        sa.Column('channel_user_id', sa.String(), nullable=False, comment="Platform user id (Slack Uxxx, Lark open_id, QQ number). Legacy wire payloads carry it as `user_id`; the schema's before-validator migrates that shape."),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('nickname', sa.String(), nullable=True),
        sa.Column('gender', sa.String(), nullable=True),
        sa.Column('age', sa.Integer(), nullable=True),
        sa.Column('title', sa.String(), nullable=True),
        sa.Column('user_id', sa.Uuid(), nullable=True, comment='The owning User — the link itself, None until proven.'),
        sa.Column('method', sa.String(), nullable=True, comment='How the link was proven (config | code); set iff user_id is.'),
        sa.Column('verified_at', sa.DateTime(), nullable=True, comment='When the link was proven; set iff user_id is.'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_user_profiles_user_id', ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('channel_tentacle_id', 'channel_user_id', name='uq_user_profiles_channel_identity'),
    )
    op.create_index(op.f('ix_user_profiles_channel_tentacle_id'), 'user_profiles', ['channel_tentacle_id'], unique=False)
    op.create_index(op.f('ix_user_profiles_channel_user_id'), 'user_profiles', ['channel_user_id'], unique=False)
    op.create_index(op.f('ix_user_profiles_user_id'), 'user_profiles', ['user_id'], unique=False)

    op.add_column('thread_messages', sa.Column('sender_id', sa.Uuid(), nullable=True, comment="The sender's registry profile row — inbound: the platform account; outbound: the channel bot or a native session's pseudo-user."))
    bind = op.get_bind()
    # Uuid columns store 32-hex on SQLite; raw SQL keeps the same format.
    rows = bind.execute(sa.text(
        "SELECT m.id, m.sender, t.channel_tentacle_id "
        "FROM thread_messages m JOIN threads t ON m.thread_id = t.id"
    )).fetchall()
    profiles: dict[tuple[str, str], str] = {}
    for message_id, blob, channel_id in rows:
        data = json.loads(blob) if isinstance(blob, str) else (blob or {})
        platform_id = str(data.get('channel_user_id') or data.get('user_id') or '0')
        key = (channel_id, platform_id)
        profile_id = profiles.get(key)
        age = data.get('age')
        if profile_id is None:
            profile_id = uuid7().hex
            profiles[key] = profile_id
            bind.execute(
                sa.text(
                    'INSERT INTO user_profiles (id, channel_tentacle_id, '
                    'channel_user_id, name, nickname, gender, age, title) '
                    'VALUES (:id, :channel, :platform, :name, :nickname, '
                    ':gender, :age, :title)'
                ),
                {
                    'id': profile_id,
                    'channel': channel_id,
                    'platform': platform_id,
                    'name': str(data.get('name') or ''),
                    'nickname': data.get('nickname'),
                    'gender': data.get('gender'),
                    'age': int(age) if age is not None else None,
                    'title': data.get('title'),
                },
            )
        bind.execute(
            sa.text('UPDATE thread_messages SET sender_id = :sender_id WHERE id = :id'),
            {'sender_id': profile_id, 'id': message_id},
        )
    with op.batch_alter_table('thread_messages', schema=None) as batch_op:
        batch_op.alter_column('sender_id', existing_type=sa.Uuid(), nullable=False)
        batch_op.create_index(batch_op.f('ix_thread_messages_sender_id'), ['sender_id'], unique=False)
        batch_op.create_foreign_key('fk_thread_messages_sender_id', 'user_profiles', ['sender_id'], ['id'])
        batch_op.drop_column('sender')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('thread_messages', sa.Column('sender', sa.JSON(), nullable=True))
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        'SELECT m.id, p.channel_user_id, p.name, p.nickname, p.gender, p.age, p.title '
        'FROM thread_messages m JOIN user_profiles p ON m.sender_id = p.id'
    )).fetchall()
    for message_id, platform_id, name, nickname, gender, age, title in rows:
        blob = json.dumps({
            'user_id': platform_id,
            'name': name,
            'nickname': nickname,
            'gender': gender,
            'age': age,
            'title': title,
        })
        bind.execute(
            sa.text('UPDATE thread_messages SET sender = :sender WHERE id = :id'),
            {'sender': blob, 'id': message_id},
        )
    with op.batch_alter_table('thread_messages', schema=None) as batch_op:
        batch_op.alter_column('sender', existing_type=sa.JSON(), nullable=False)
        batch_op.drop_constraint('fk_thread_messages_sender_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_thread_messages_sender_id'))
        batch_op.drop_column('sender_id')

    op.drop_index(op.f('ix_user_profiles_user_id'), table_name='user_profiles')
    op.drop_index(op.f('ix_user_profiles_channel_user_id'), table_name='user_profiles')
    op.drop_index(op.f('ix_user_profiles_channel_tentacle_id'), table_name='user_profiles')
    op.drop_table('user_profiles')
    op.drop_table('users')
