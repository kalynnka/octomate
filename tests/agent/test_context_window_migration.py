"""Context schema and backfill upgrade together without guessing historical counts."""

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.usage import RequestUsage
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.config.database import database_settings
from octomate.managers import ConversationManager
from octomate.types.json import JsonObject
from tests.support.managers import a_thread

PREVIOUS = "294f429c72cb"
REVISION = "1a83da0aa445"


@dataclass
class BackfillCase:
    agent: str
    provider: str
    usages: list[RequestUsage]
    expected_inputs: list[int]
    context_used: int | None
    context_window: int | None = None
    metadata: JsonObject | None = None


DEEPSEEK_METADATA: JsonObject = {
    "source": "deepseek",
    "events": [
        {"type": "request/context", "data": {"contextWindow": 128000}},
        {
            "type": "assistant/message",
            "data": {
                "usage": {
                    "inputTokens": 100,
                    "cacheReadTokens": 20,
                    "cacheWriteTokens": 5,
                }
            },
        },
    ],
}


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            BackfillCase(
                "claude-native",
                "anthropic",
                [
                    RequestUsage(input_tokens=3, cache_read_tokens=100),
                    RequestUsage(
                        input_tokens=7, cache_read_tokens=200, cache_write_tokens=10
                    ),
                ],
                [103, 217],
                217,
            ),
            id="legacy-claude-last-request-includes-both-caches",
        ),
        pytest.param(
            BackfillCase(
                "claude",
                "anthropic",
                [RequestUsage(input_tokens=100, cache_read_tokens=20)],
                [100],
                None,
            ),
            id="ambiguous-claude-input-is-not-guessed",
        ),
        pytest.param(
            BackfillCase(
                "claude-native",
                "anthropic",
                [RequestUsage(input_tokens=100)],
                [100],
                100,
            ),
            id="uncached-input-needs-no-normalization",
        ),
        pytest.param(
            BackfillCase(
                "deepseek",
                "deepseek",
                [
                    RequestUsage(
                        input_tokens=100, cache_read_tokens=20, cache_write_tokens=5
                    )
                ],
                [125],
                125,
                128000,
                DEEPSEEK_METADATA,
            ),
            id="deepseek-wire-proves-exclusive-input-even-above-cache",
        ),
        pytest.param(
            BackfillCase(
                "deepseek",
                "deepseek",
                [
                    RequestUsage(
                        input_tokens=125, cache_read_tokens=20, cache_write_tokens=5
                    )
                ],
                [125],
                125,
                128000,
                DEEPSEEK_METADATA,
            ),
            id="already-normalized-deepseek-is-not-counted-twice",
        ),
        pytest.param(
            BackfillCase(
                "deepseek",
                "deepseek",
                [RequestUsage(input_tokens=3, cache_read_tokens=100)],
                [103],
                103,
            ),
            id="legacy-deepseek-without-wire-usage",
        ),
        pytest.param(
            BackfillCase(
                "inkling",
                "deepseek",
                [RequestUsage(input_tokens=100, cache_read_tokens=20)],
                [100],
                100,
            ),
            id="pydantic-deepseek-input-is-already-inclusive",
        ),
        pytest.param(
            BackfillCase(
                "inkling",
                "anthropic",
                [RequestUsage(input_tokens=100, cache_read_tokens=20)],
                [100],
                100,
            ),
            id="pydantic-anthropic-input-is-already-inclusive",
        ),
        pytest.param(
            BackfillCase(
                "codex-native",
                "openai_codex",
                [
                    RequestUsage(
                        input_tokens=100,
                        cache_read_tokens=20,
                        details={"model_context_window": 262144},
                    )
                ],
                [100],
                100,
                262144,
            ),
            id="codex-inclusive-input-and-recorded-capacity",
        ),
        pytest.param(
            BackfillCase(
                "codex",
                "openai_codex",
                [RequestUsage(input_tokens=100), RequestUsage()],
                [100, 0],
                None,
            ),
            id="unmeasured-last-response-does-not-reuse-an-earlier-input",
        ),
        pytest.param(
            BackfillCase(
                "codex",
                "openai_codex",
                [RequestUsage()],
                [0],
                None,
            ),
            id="empty-usage-is-not-a-measured-zero",
        ),
        pytest.param(
            BackfillCase(
                "codex",
                "openai_codex",
                [RequestUsage(input_tokens=0, output_tokens=1)],
                [0],
                0,
            ),
            id="reported-zero-input-is-retained",
        ),
        pytest.param(
            BackfillCase(
                "codex_acp",
                "acp",
                [RequestUsage()],
                [0],
                90,
                1000000,
                {
                    "source": "acp",
                    "updates": [
                        {"sessionUpdate": "usage_update", "used": 100, "size": 128000},
                        {"sessionUpdate": "usage_update", "used": 90, "size": 1000000},
                    ],
                },
            ),
            id="acp-latest-snapshot-does-not-invent-billing",
        ),
        pytest.param(
            BackfillCase(
                "codex_acp",
                "acp",
                [RequestUsage()],
                [0],
                0,
                metadata={
                    "source": "acp",
                    "updates": [
                        {"sessionUpdate": "usage_update", "used": 0, "size": 0}
                    ],
                },
            ),
            id="acp-zero-used-and-unknown-window",
        ),
        pytest.param(
            BackfillCase(
                "codex_acp",
                "acp",
                [RequestUsage()],
                [0],
                None,
                metadata={
                    "source": "acp",
                    "updates": [
                        {"sessionUpdate": "usage_update", "used": True, "size": -1}
                    ],
                },
            ),
            id="malformed-counts-stay-unknown",
        ),
        pytest.param(
            BackfillCase(
                "deepseek",
                "deepseek",
                [RequestUsage(input_tokens=10)],
                [10],
                10,
                metadata={
                    "source": "deepseek",
                    "events": [
                        {"type": "request/context", "data": {"contextWindow": 128000}},
                        {"type": "request/context", "data": {"model": "changed-route"}},
                    ],
                },
            ),
            id="route-without-capacity-clears-an-older-report",
        ),
    ],
)
async def test_context_schema_and_backfill_preserve_history_on_a_copy(
    case: BackfillCase,
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ConversationManager()
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id=case.agent)
    await manager.record_agent_run(
        conversation,
        run_id=str(uuid7()),
        messages=[
            ModelResponse(
                parts=[TextPart(f"saved answer {index}")],
                provider_name=case.provider,
                model_name="saved-model",
                usage=usage,
                metadata=case.metadata if index == len(case.usages) - 1 else None,
            )
            for index, usage in enumerate(case.usages)
        ],
    )
    source = await asyncio.to_thread(Path(in_memory_engine.url.database or "").resolve)
    assert source.is_relative_to(await asyncio.to_thread(tmp_path.resolve))
    target = tmp_path / "context-migration-copy.db"
    with (
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original,
        sqlite3.connect(target) as copied,
    ):
        original.backup(copied)
        copied.execute("ALTER TABLE agent_runs DROP COLUMN context_used")
        copied.execute("ALTER TABLE agent_runs DROP COLUMN context_window")
        copied.execute("UPDATE model_messages SET history_digest='preserved-digest'")
        before_runs = copied.execute("SELECT * FROM agent_runs ORDER BY id").fetchall()
        columns = [row[1] for row in copied.execute("PRAGMA table_info(agent_runs)")]
        message_columns = [
            row[1]
            for row in copied.execute("PRAGMA table_info(model_messages)")
            if row[1] != "usage"
        ]
        names = ", ".join(f'"{name}"' for name in message_columns)
        before_messages = copied.execute(
            f"SELECT {names} FROM model_messages ORDER BY id"
        ).fetchall()
        before_usage = [
            json.loads(row[0])
            for row in copied.execute("SELECT usage FROM model_messages ORDER BY id")
        ]

    monkeypatch.setattr(database_settings, "db_url", f"sqlite+aiosqlite:///{target}")
    actual = await asyncio.to_thread(
        Path(make_url(database_settings.db_url).database or "").resolve
    )
    assert actual == await asyncio.to_thread(target.resolve)
    assert actual != source
    module_path = await asyncio.to_thread(Path(__file__).resolve)
    config = Config(str(module_path.parents[2] / "alembic.ini"))
    assert ScriptDirectory.from_config(config).get_heads() == [REVISION]
    # Alembic's logging setup would disable pytest's existing loggers.
    monkeypatch.setattr("logging.config.fileConfig", Mock())
    await asyncio.to_thread(command.stamp, config, PREVIOUS)
    await asyncio.to_thread(command.upgrade, config, "head")
    with sqlite3.connect(target) as copied:
        assert copied.execute(
            "SELECT context_used, context_window FROM agent_runs"
        ).fetchall() == [(case.context_used, case.context_window)]
        assert copied.execute("PRAGMA foreign_key_check").fetchall() == []
        run_names = ", ".join(f'"{name}"' for name in columns)
        assert (
            copied.execute(f"SELECT {run_names} FROM agent_runs ORDER BY id").fetchall()
            == before_runs
        )
        assert (
            copied.execute(f"SELECT {names} FROM model_messages ORDER BY id").fetchall()
            == before_messages
        )
        after_usage = [
            json.loads(row[0])
            for row in copied.execute("SELECT usage FROM model_messages ORDER BY id")
        ]
        for before, after, expected in zip(
            before_usage, after_usage, case.expected_inputs, strict=True
        ):
            assert after == {**before, "input_tokens": expected}

    await asyncio.to_thread(command.downgrade, config, PREVIOUS)
    with sqlite3.connect(target) as copied:
        assert (
            copied.execute("SELECT * FROM agent_runs ORDER BY id").fetchall()
            == before_runs
        )
        assert (
            copied.execute(f"SELECT {names} FROM model_messages ORDER BY id").fetchall()
            == before_messages
        )
        assert [
            json.loads(row[0])
            for row in copied.execute("SELECT usage FROM model_messages ORDER BY id")
        ] == after_usage
        assert copied.execute("PRAGMA foreign_key_check").fetchall() == []
