"""user identity registry

Create YAML-owned users and their observed channel profiles. Profiles without
a configured owner remain visitors. Thread-message sender snapshots move from
JSON into durable profile rows.

Revision ID: f4a6f02876d3
Revises: 417719624acf
Create Date: 2026-07-24 23:37:35.654916

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from uuid_utils.compat import uuid7

# revision identifiers, used by Alembic.
revision: str = "f4a6f02876d3"
down_revision: str | Sequence[str] | None = "417719624acf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "username",
            sa.String(),
            nullable=False,
            comment="Stable username naming this human — the `users:` YAML key.",
        ),
        sa.Column(
            "name",
            sa.String(),
            nullable=False,
            comment="Canonical display name the agent uses for this human.",
        ),
        sa.Column(
            "nickname",
            sa.String(),
            nullable=True,
            comment="A shorter, casual name for this human.",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "user_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "channel_tentacle_id",
            sa.String(),
            nullable=False,
            comment=(
                "The channel this identity lives on; with channel_user_id it is "
                "the profile's identity — one row per (channel, platform user) "
                "ever seen."
            ),
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            nullable=True,
            comment="The YAML-declared owning User; NULL for a visitor profile.",
        ),
        sa.Column(
            "channel_user_id",
            sa.String(),
            nullable=False,
            comment="Platform user id (Slack Uxxx, Lark open_id, QQ number).",
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("nickname", sa.String(), nullable=True),
        sa.Column("gender", sa.String(), nullable=True),
        sa.Column("age", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel_tentacle_id",
            "channel_user_id",
            name="uq_user_profiles_channel_identity",
        ),
    )
    with op.batch_alter_table("user_profiles", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_user_profiles_channel_tentacle_id"),
            ["channel_tentacle_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_user_profiles_channel_user_id"),
            ["channel_user_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_user_profiles_user_id"), ["user_id"], unique=False
        )

    op.add_column(
        "thread_messages",
        sa.Column(
            "sender_id",
            sa.Uuid(),
            nullable=True,
            comment=(
                "The sender's registry profile row — inbound: the platform "
                "account; outbound: the channel bot or a native session's "
                "pseudo-user."
            ),
        ),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT m.id, m.sender, t.channel_tentacle_id "
            "FROM thread_messages m JOIN threads t ON m.thread_id = t.id "
            "ORDER BY m.happened_at, m.id"
        )
    ).fetchall()
    profiles: dict[tuple[str, str], str] = {}
    for message_id, blob, channel_id in rows:
        data = json.loads(blob) if isinstance(blob, str) else (blob or {})
        raw_platform_id = data.get("channel_user_id")
        if raw_platform_id is None:
            raise ValueError(
                f"thread message {message_id} sender has no channel_user_id"
            )
        platform_id = str(raw_platform_id)
        key = (channel_id, platform_id)
        profile_id = profiles.get(key)
        age = data.get("age")
        values = {
            "name": str(data.get("name") or ""),
            "nickname": data.get("nickname"),
            "gender": data.get("gender"),
            "age": int(age) if age is not None else None,
            "title": data.get("title"),
        }
        if profile_id is None:
            profile_id = uuid7().hex
            profiles[key] = profile_id
            bind.execute(
                sa.text(
                    "INSERT INTO user_profiles (id, channel_tentacle_id, "
                    "channel_user_id, name, nickname, gender, age, title) "
                    "VALUES (:id, :channel, :platform, :name, :nickname, "
                    ":gender, :age, :title)"
                ),
                {
                    "id": profile_id,
                    "channel": channel_id,
                    "platform": platform_id,
                    **values,
                },
            )
        else:
            bind.execute(
                sa.text(
                    "UPDATE user_profiles SET name = :name, nickname = :nickname, "
                    "gender = :gender, age = :age, title = :title WHERE id = :id"
                ),
                {"id": profile_id, **values},
            )
        bind.execute(
            sa.text("UPDATE thread_messages SET sender_id = :sender_id WHERE id = :id"),
            {"sender_id": profile_id, "id": message_id},
        )

    with op.batch_alter_table("thread_messages", schema=None) as batch_op:
        batch_op.alter_column("sender_id", existing_type=sa.Uuid(), nullable=False)
        batch_op.create_index(
            batch_op.f("ix_thread_messages_sender_id"),
            ["sender_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_thread_messages_sender_id",
            "user_profiles",
            ["sender_id"],
            ["id"],
        )
        batch_op.drop_column("sender")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("thread_messages", sa.Column("sender", sa.JSON(), nullable=True))
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT m.id, p.channel_user_id, p.name, p.nickname, p.gender, "
            "p.age, p.title FROM thread_messages m "
            "JOIN user_profiles p ON m.sender_id = p.id"
        )
    ).fetchall()
    for message_id, platform_id, name, nickname, gender, age, title in rows:
        blob = json.dumps(
            {
                "channel_user_id": platform_id,
                "name": name,
                "nickname": nickname,
                "gender": gender,
                "age": age,
                "title": title,
            }
        )
        bind.execute(
            sa.text("UPDATE thread_messages SET sender = :sender WHERE id = :id"),
            {"sender": blob, "id": message_id},
        )
    with op.batch_alter_table("thread_messages", schema=None) as batch_op:
        batch_op.alter_column("sender", existing_type=sa.JSON(), nullable=False)
        batch_op.drop_constraint("fk_thread_messages_sender_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_thread_messages_sender_id"))
        batch_op.drop_column("sender_id")

    with op.batch_alter_table("user_profiles", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_profiles_user_id"))
        batch_op.drop_index(batch_op.f("ix_user_profiles_channel_user_id"))
        batch_op.drop_index(batch_op.f("ix_user_profiles_channel_tentacle_id"))

    op.drop_table("user_profiles")
    op.drop_table("users")
