"""Normalize stored harness messages into clean text and optional annotations.

Only existing JSON payloads and their searchable text change. Original harness
text stays in each annotation. Both bound and unbound ledger rows are included.
Downgrade retains this lossless data normalization; there is no schema to undo.

Revision ID: 90e954ec0b2d
Revises: 792e14ef4f19
Create Date: 2026-11-13 01:23:00

"""

from collections.abc import Sequence
from typing import Literal, TypedDict

import sqlalchemy as sa
from alembic import op
from pydantic import JsonValue, TypeAdapter

from octomate.schemas.annotations import MessageAnnotations
from octomate.tentacles.claude.content import prompt_content as claude_prompt
from octomate.tentacles.codex.content import answer_part
from octomate.tentacles.codex.content import prompt_content as codex_prompt

# revision identifiers, used by Alembic.
revision: str = "90e954ec0b2d"
down_revision: str | Sequence[str] | None = "792e14ef4f19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


type JsonObject = dict[str, JsonValue]


class MessageRow(TypedDict):
    id: str
    content: list[JsonObject]
    message_text: str | None
    source: Literal["codex", "claude"]
    prompt: bool | None


row_adapter = TypeAdapter(MessageRow)
object_adapter = TypeAdapter(JsonObject)
markers = (
    "# Context from my IDE setup:",
    "<ide_opened_file>",
    "<ide_selection>",
    "<browser_instruction>",
    "<oai-mem-citation>",
)
models = sa.table(
    "model_messages",
    sa.column("id", sa.String()),
    sa.column("run_id", sa.String()),
    sa.column("parts", sa.JSON()),
    sa.column("message_text", sa.String()),
)
runs = sa.table(
    "agent_runs",
    sa.column("id", sa.String()),
    sa.column("conversation_id", sa.String()),
)
conversations = sa.table(
    "conversations",
    sa.column("id", sa.String()),
    sa.column("agent_tentacle_id", sa.String()),
)
ledger = sa.table(
    "thread_messages",
    sa.column("id", sa.String()),
    sa.column("thread_id", sa.String()),
    sa.column("agent_tentacle_id", sa.String()),
    sa.column("actor_kind", sa.String()),
    sa.column("segments", sa.JSON()),
    sa.column("message_text", sa.String()),
)
threads = sa.table(
    "threads",
    sa.column("id", sa.String()),
    sa.column("channel_tentacle_id", sa.String()),
)


def annotated_text(
    text: str, source: Literal["codex", "claude"], *, prompt: bool
) -> tuple[str, JsonObject] | None:
    if prompt:
        content = codex_prompt(text) if source == "codex" else claude_prompt(text)
        if isinstance(content, str):
            return None
        return content.content, MessageAnnotations.model_validate(
            content.metadata["annotations"]
        ).model_dump(mode="json")
    if source != "codex":
        return None
    answer = answer_part(text)
    if not answer.provider_details:
        return None
    return answer.content, MessageAnnotations.model_validate(
        answer.provider_details["annotations"]
    ).model_dump(mode="json")


def normalize(row: MessageRow) -> bool:
    changed = False
    for part in row["content"]:
        if row["prompt"] is None:
            if part.get("part_kind") not in {"user-prompt", "text"}:
                continue
            data, key, prompt = part, "content", part["part_kind"] == "user-prompt"
        else:
            payload = part.get("data")
            if part.get("type") not in {"text", "markdown"} or not isinstance(
                payload, dict
            ):
                continue
            data, key, prompt = payload, "text", row["prompt"]
        raw = data.get(key)
        if not isinstance(raw, str):
            continue
        result = annotated_text(raw, row["source"], prompt=prompt)
        if result is None:
            continue
        text, details = result
        if row["message_text"] is not None:
            row["message_text"] = row["message_text"].replace(raw, text)
        if row["prompt"] is not None:
            data.update(text=text, annotations=details)
        elif prompt:
            part["content"] = [
                {
                    "kind": "text-content",
                    "content": text,
                    "metadata": {"annotations": details},
                }
            ]
        else:
            provider = part.get("provider_details")
            part.update(
                content=text,
                provider_name="openai_codex",
                provider_details={
                    **object_adapter.validate_python(provider or {}),
                    "annotations": details,
                },
            )
        changed = True
    return changed


def upgrade() -> None:
    """Normalize persisted data on Alembic's connection, without runtime config."""
    source = sa.case(
        (
            ledger.c.agent_tentacle_id.in_(("codex", "claude")),
            ledger.c.agent_tentacle_id,
        ),
        else_=threads.c.channel_tentacle_id,
    )
    queries = (
        (
            models,
            "parts",
            sa.select(
                models.c.id,
                models.c.parts.label("content"),
                models.c.message_text,
                conversations.c.agent_tentacle_id.label("source"),
                sa.null().label("prompt"),
            )
            .select_from(
                models.join(runs, models.c.run_id == runs.c.id).join(
                    conversations, runs.c.conversation_id == conversations.c.id
                )
            )
            .where(conversations.c.agent_tentacle_id.in_(("codex", "claude"))),
        ),
        (
            ledger,
            "segments",
            sa.select(
                ledger.c.id,
                ledger.c.segments.label("content"),
                ledger.c.message_text,
                source.label("source"),
                (ledger.c.actor_kind == "human").label("prompt"),
            )
            .select_from(ledger.join(threads, ledger.c.thread_id == threads.c.id))
            .where(source.in_(("codex", "claude")), ledger.c.actor_kind != "system"),
        ),
    )
    bind = op.get_bind()
    rows = (
        (table, column, value)
        for table, column, query in queries
        for value in bind.execute(
            query.where(
                sa.or_(*(table.c.message_text.contains(marker) for marker in markers))
            )
        ).mappings()
    )
    with bind.begin_nested():
        for table, column, value in rows:
            row = row_adapter.validate_python(value)
            if not normalize(row):
                continue
            bind.execute(
                table.update()
                .where(table.c.id == row["id"])
                .values({column: row["content"], "message_text": row["message_text"]})
            )


def downgrade() -> None:
    """Keep the normalized data and its preserved original text."""
