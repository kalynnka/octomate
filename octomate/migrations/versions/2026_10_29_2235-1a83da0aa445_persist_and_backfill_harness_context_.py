"""Persist context snapshots and recover the counts recorded by older harnesses.

Legacy adapters stored cache-exclusive input. Correct it only when raw usage
or an input smaller than its cache buckets proves that representation. Leave
ambiguous counts and unreported capacities unknown. Downgrade keeps corrected
response usage: removing context columns does not change token accounting.

Revision ID: 1a83da0aa445
Revises: 294f429c72cb
Create Date: 2026-10-29 22:35:19.802901

"""

from collections.abc import Sequence
from typing import TypedDict

import sqlalchemy as sa
from alembic import op
from pydantic import JsonValue, TypeAdapter

# revision identifiers, used by Alembic.
revision: str = "1a83da0aa445"
down_revision: str | Sequence[str] | None = "294f429c72cb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

type JsonObject = dict[str, JsonValue]


class MessageRow(TypedDict):
    id: str
    run_id: str
    kind: str
    provider_name: str | None
    agent_tentacle_id: str
    usage: JsonObject | None
    metadata: JsonObject | None


class ContextSnapshot(TypedDict, total=False):
    context_used: int | None
    context_window: int | None


message_adapter = TypeAdapter(MessageRow)
messages = sa.table(
    "model_messages",
    sa.column("id", sa.String()),
    sa.column("run_id", sa.String()),
    sa.column("kind", sa.String()),
    sa.column("provider_name", sa.String()),
    sa.column("usage", sa.JSON()),
    sa.column("metadata", sa.JSON()),
)
runs = sa.table(
    "agent_runs",
    sa.column("id", sa.String()),
    sa.column("conversation_id", sa.String()),
    sa.column("context_used", sa.Integer()),
    sa.column("context_window", sa.Integer()),
)
conversations = sa.table(
    "conversations",
    sa.column("id", sa.String()),
    sa.column("agent_tentacle_id", sa.String()),
)


def token_count(value: JsonValue) -> int | None:
    """A reported whole token count, including zero but excluding booleans."""
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def deepseek_input(metadata: JsonObject, read: int, write: int) -> int | None:
    """Recover a response's input from its cache-exclusive wire usage."""
    events = metadata.get("events")
    if not isinstance(events, list):
        return None
    result = None
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "assistant/message":
            continue
        data = event.get("data")
        raw = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            continue
        uncached = token_count(raw.get("inputTokens"))
        if (
            uncached is not None
            and token_count(raw.get("cacheReadTokens", 0)) == read
            and token_count(raw.get("cacheWriteTokens", 0)) == write
        ):
            result = uncached + read + write
    return result


def normalized_input(row: MessageRow) -> int | None:
    """Normalize identifiable legacy adapter input without adding caches twice."""
    usage = row["usage"]
    if not usage or not any(
        usage.get(key)
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        )
    ):
        return None
    count = token_count(usage.get("input_tokens"))
    read = token_count(usage.get("cache_read_tokens", 0))
    write = token_count(usage.get("cache_write_tokens", 0))
    if count is None or read is None or write is None:
        return None
    legacy = (
        row["agent_tentacle_id"] in ("claude", "claude-native")
        and row["provider_name"] == "anthropic"
    ) or (row["agent_tentacle_id"] == "deepseek" and row["provider_name"] == "deepseek")
    if not legacy or not read + write:
        return count
    raw_input = (
        deepseek_input(row["metadata"] or {}, read, write)
        if row["agent_tentacle_id"] == "deepseek"
        else None
    )
    if raw_input is not None:
        count = raw_input
    elif count < read + write:
        count += read + write
    else:
        return None
    usage["input_tokens"] = count
    return count


def reported_context(event: JsonValue) -> ContextSnapshot:
    """Read explicit occupancy and capacity reports retained as replay metadata."""
    if not isinstance(event, dict):
        return {}
    if event.get("sessionUpdate") == "usage_update":
        return {
            "context_used": token_count(event.get("used")),
            "context_window": token_count(event.get("size")) or None,
        }
    if event.get("type") != "request/context":
        return {}
    data = event.get("data")
    if not isinstance(data, dict):
        return {}
    return {"context_window": token_count(data.get("contextWindow")) or None}


def upgrade() -> None:
    """Upgrade schema."""
    connection = op.get_bind()
    rows = [
        message_adapter.validate_python(row)
        for row in connection.execute(
            sa.select(messages, conversations.c.agent_tentacle_id)
            .join(runs, runs.c.id == messages.c.run_id)
            .join(conversations, conversations.c.id == runs.c.conversation_id)
            .order_by(messages.c.id)
        ).mappings()
    ]
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "context_window",
                sa.Integer(),
                nullable=True,
                comment="Context capacity in tokens reported by the harness for this run; NULL when unknown.",
            )
        )
        batch_op.add_column(
            sa.Column(
                "context_used",
                sa.Integer(),
                nullable=True,
                comment="Context tokens used, from the harness occupancy snapshot or the last model request's input usage; NULL when unavailable.",
            )
        )

    contexts: dict[str, ContextSnapshot] = {}
    for row in rows:
        context = contexts.setdefault(row["run_id"], {})
        usage = row["usage"] or {}
        original_input = usage.get("input_tokens")
        if row["kind"] == "response" and row["provider_name"] != "acp":
            context["context_used"] = normalized_input(row)
            if usage.get("input_tokens") != original_input:
                connection.execute(
                    messages.update()
                    .where(messages.c.id == row["id"])
                    .values(usage=usage)
                )
        details = usage.get("details")
        if isinstance(details, dict) and "model_context_window" in details:
            context["context_window"] = (
                token_count(details["model_context_window"]) or None
            )
        metadata = row["metadata"] or {}
        for events in (metadata.get("events"), metadata.get("updates")):
            if not isinstance(events, list):
                continue
            for event in events:
                context.update(reported_context(event))
    for run_id, context in contexts.items():
        if context:
            connection.execute(
                runs.update().where(runs.c.id == run_id).values(**context)
            )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.drop_column("context_used")
        batch_op.drop_column("context_window")
