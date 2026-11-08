"""Assign native history to default runtime agents without changing its provenance.

Ownership, thread addresses, sender profiles, deferred-action addresses, and run
names use the runtime agents. Session/turn IDs and the persisted native thread kind
remain unchanged. The redundant run runtime namespace and its index are removed.
Duplicate native addresses keep the canonical thread and conversation ids. Their
histories are combined, identical deliveries are coalesced with their bindings,
and the canonical settings and transcript reference take precedence. Conflicting
content or frozen thread ledgers abort the migration without changing history.

Revision ID: 792e14ef4f19
Revises: 1a83da0aa445
Create Date: 2026-11-05 22:19:40.105461

"""

from collections.abc import Sequence
from typing import ClassVar

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection, RowMapping

# revision identifiers, used by Alembic.
revision: str = "792e14ef4f19"
down_revision: str | Sequence[str] | None = "1a83da0aa445"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class NativeHistoryMerge:
    references: ClassVar[dict[str, tuple[tuple[str, str], ...]]] = {
        "user_profiles": (
            ("thread_messages", "sender_id"),
            ("oauth_operations", "profile_id"),
            ("profile_binding_sessions", "profile_id"),
        ),
        "conversations": (
            ("agent_runs", "conversation_id"),
            ("conversation_runs", "conversation_id"),
            ("conversations", "parent_conversation_id"),
            ("deferred_action_batches", "conversation_id"),
            ("todos", "conversation_id"),
            ("thread_messages", "conversation_id"),
            ("channel_handoffs", "source_conversation_id"),
            ("channel_handoffs", "target_conversation_id"),
        ),
        "thread_messages": (
            ("message_binding", "thread_message_id"),
            ("thread_ledgers", "cut_message_id"),
            ("threads", "source_cursor_message_id"),
        ),
        "threads": (
            ("threads", "parent_thread_id"),
            ("conversations", "thread_id"),
            ("thread_messages", "thread_id"),
            ("channel_handoffs", "thread_id"),
            ("thread_ledgers", "thread_id"),
            ("thread_ledgers", "ledger_id"),
        ),
    }
    bind: Connection
    owners: dict[str, str]
    tables: dict[str, sa.Table]

    def __init__(self, bind: Connection, owners: dict[str, str]) -> None:
        self.bind = bind
        self.owners = owners
        metadata = sa.MetaData()
        self.tables = {
            name: sa.Table(name, metadata, autoload_with=bind)
            for name in (
                "threads",
                "conversations",
                "user_profiles",
                "thread_messages",
                "thread_ledgers",
                "conversation_runs",
                "message_binding",
                "todos",
            )
        }

    def repoint(self, entity: str, source_id: str, target_id: str) -> None:
        for name, column in self.references[entity]:
            table = sa.table(name, sa.column(column))
            self.bind.execute(
                table.update()
                .where(table.c[column] == source_id)
                .values({column: target_id})
            )

    def profiles(self, native_id: str, owner: str) -> None:
        table = self.tables["user_profiles"]
        sources = (
            self.bind.execute(
                sa.select(table).where(table.c.channel_tentacle_id == native_id)
            )
            .mappings()
            .all()
        )
        for source in sources:
            target = (
                self.bind.execute(
                    sa.select(table).where(
                        table.c.channel_tentacle_id == owner,
                        table.c.channel_user_id == source["channel_user_id"],
                    )
                )
                .mappings()
                .first()
            )
            if target is None:
                continue
            if (
                source["user_id"]
                and target["user_id"]
                and source["user_id"] != target["user_id"]
            ):
                raise RuntimeError(
                    "Cannot merge native profiles linked to different users"
                )
            missing = {
                column.name: source[column.name]
                for column in table.c
                if target[column.name] is None and source[column.name] is not None
            }
            if missing:
                self.bind.execute(
                    table.update().where(table.c.id == target["id"]).values(missing)
                )
            self.repoint("user_profiles", source["id"], target["id"])
            self.bind.execute(table.delete().where(table.c.id == source["id"]))

    def conversation(self, source: RowMapping, target: RowMapping) -> None:
        if (
            source["external_id"]
            and target["external_id"]
            and source["external_id"] != target["external_id"]
        ):
            raise RuntimeError(
                "Cannot merge conversations from different native sessions"
            )
        todos = self.tables["todos"]
        for todo in (
            self.bind.execute(
                sa.select(todos).where(todos.c.conversation_id == source["id"])
            )
            .mappings()
            .all()
        ):
            existing = (
                self.bind.execute(
                    sa.select(todos).where(
                        todos.c.conversation_id == target["id"],
                        todos.c.ref == todo["ref"],
                    )
                )
                .mappings()
                .first()
            )
            if existing is None:
                continue
            if any(
                todo[column.name] != existing[column.name]
                for column in todos.c
                if column.name
                not in {"id", "conversation_id", "created_at", "updated_at"}
            ):
                raise RuntimeError("Cannot merge conflicting native todos")
            self.bind.execute(todos.delete().where(todos.c.id == todo["id"]))
        history = self.tables["conversation_runs"]
        shared = sa.select(history.c.run_id).where(
            history.c.conversation_id == target["id"]
        )
        self.bind.execute(
            history.delete().where(
                history.c.conversation_id == source["id"], history.c.run_id.in_(shared)
            )
        )
        self.repoint("conversations", source["id"], target["id"])
        table = self.tables["conversations"]
        missing = {
            column: source[column]
            for column in (
                "external_id",
                "name",
                "transcript_file_id",
                "permission_mode",
                "effort",
            )
            if target[column] is None and source[column] is not None
        }
        if missing:
            self.bind.execute(
                table.update().where(table.c.id == target["id"]).values(missing)
            )
        self.bind.execute(table.delete().where(table.c.id == source["id"]))

    def message(self, source: RowMapping, target: RowMapping) -> None:
        messages = self.tables["thread_messages"]
        for column in messages.c:
            if column.name in {"id", "thread_id", "created_at"}:
                continue
            before, after = source[column.name], target[column.name]
            if column.name == "agent_tentacle_id":
                before = self.owners.get(before, before)
            if before != after:
                raise RuntimeError(
                    f"Cannot merge conflicting native deliveries: {column.name}"
                )
        bindings = self.tables["message_binding"]
        for binding in (
            self.bind.execute(
                sa.select(bindings).where(bindings.c.thread_message_id == source["id"])
            )
            .mappings()
            .all()
        ):
            existing = (
                self.bind.execute(
                    sa.select(bindings).where(
                        bindings.c.thread_message_id == target["id"],
                        bindings.c.model_message_id == binding["model_message_id"],
                        bindings.c.kind == binding["kind"],
                    )
                )
                .mappings()
                .first()
            )
            if existing is None:
                continue
            if any(
                binding[key] != existing[key]
                for key in ("run_id", "tool_call_id", "position")
            ):
                raise RuntimeError("Cannot merge conflicting native message bindings")
            self.bind.execute(
                bindings.delete().where(
                    bindings.c.thread_message_id == source["id"],
                    bindings.c.model_message_id == binding["model_message_id"],
                    bindings.c.kind == binding["kind"],
                )
            )
        self.repoint("thread_messages", source["id"], target["id"])
        self.bind.execute(messages.delete().where(messages.c.id == source["id"]))

    def thread(self, source: RowMapping, target: RowMapping) -> None:
        if source["kind"] != "native_thread" or target["kind"] != "native_thread":
            raise RuntimeError("Cannot merge non-native thread addresses")
        if (
            source["project_id"]
            and target["project_id"]
            and source["project_id"] != target["project_id"]
        ):
            raise RuntimeError("Cannot merge native threads from different projects")
        ledgers = self.tables["thread_ledgers"]
        pair = (source["id"], target["id"])
        frozen = self.bind.execute(
            sa.select(ledgers).where(
                sa.or_(ledgers.c.thread_id.in_(pair), ledgers.c.ledger_id.in_(pair)),
                ledgers.c.thread_id != ledgers.c.ledger_id,
            )
        ).first()
        if frozen is not None:
            raise RuntimeError("Cannot merge native threads with frozen fork ledgers")
        conversations = self.tables["conversations"]
        for conversation in (
            self.bind.execute(
                sa.select(conversations).where(
                    conversations.c.thread_id == source["id"]
                )
            )
            .mappings()
            .all()
        ):
            owner = self.owners.get(
                conversation["agent_tentacle_id"], conversation["agent_tentacle_id"]
            )
            existing = (
                self.bind.execute(
                    sa.select(conversations).where(
                        conversations.c.thread_id == target["id"],
                        conversations.c.agent_tentacle_id == owner,
                        conversations.c.subagent_id == conversation["subagent_id"],
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                self.conversation(conversation, existing)
        messages = self.tables["thread_messages"]
        for message in (
            self.bind.execute(
                sa.select(messages).where(
                    messages.c.thread_id == source["id"],
                    messages.c.platform_message_id.is_not(None),
                )
            )
            .mappings()
            .all()
        ):
            existing = (
                self.bind.execute(
                    sa.select(messages).where(
                        messages.c.thread_id == target["id"],
                        messages.c.platform_message_id
                        == message["platform_message_id"],
                        messages.c.direction == message["direction"],
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                self.message(message, existing)
        self.bind.execute(ledgers.delete().where(ledgers.c.thread_id == source["id"]))
        self.repoint("threads", source["id"], target["id"])
        table = self.tables["threads"]
        self.bind.execute(
            table.update()
            .where(table.c.id == target["id"])
            .values(
                title=target["title"] or source["title"],
                project_id=target["project_id"] or source["project_id"],
                source_cursor_message_id=sa.func.coalesce(
                    table.c.source_cursor_message_id,
                    sa.select(table.c.source_cursor_message_id)
                    .where(table.c.id == source["id"])
                    .scalar_subquery(),
                ),
                created_at=min(source["created_at"], target["created_at"]),
                updated_at=max(source["updated_at"], target["updated_at"]),
            )
        )
        self.bind.execute(table.delete().where(table.c.id == source["id"]))

    def merge(self, native_id: str, owner: str) -> None:
        self.profiles(native_id, owner)
        threads = self.tables["threads"]
        for source in (
            self.bind.execute(
                sa.select(threads).where(
                    threads.c.channel_tentacle_id == native_id,
                    threads.c.parent_thread_id.is_(None),
                )
            )
            .mappings()
            .all()
        ):
            target = (
                self.bind.execute(
                    sa.select(threads).where(
                        threads.c.channel_tentacle_id == owner,
                        threads.c.chat_type == source["chat_type"],
                        threads.c.chat_id == source["chat_id"],
                        threads.c.channel_thread_id.is_not_distinct_from(
                            source["channel_thread_id"]
                        ),
                        threads.c.parent_thread_id.is_(None),
                    )
                )
                .mappings()
                .first()
            )
            if target is not None:
                self.thread(source, target)
        conversations = self.tables["conversations"]
        for source in (
            self.bind.execute(
                sa.select(conversations).where(
                    conversations.c.agent_tentacle_id == native_id
                )
            )
            .mappings()
            .all()
        ):
            target = (
                self.bind.execute(
                    sa.select(conversations).where(
                        conversations.c.thread_id == source["thread_id"],
                        conversations.c.agent_tentacle_id == owner,
                        conversations.c.subagent_id == source["subagent_id"],
                    )
                )
                .mappings()
                .first()
            )
            if target is not None:
                self.conversation(source, target)


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
    with bind.begin_nested():
        merge = NativeHistoryMerge(bind, owners)
        for native_id, owner in owners.items():
            merge.merge(native_id, owner)
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
