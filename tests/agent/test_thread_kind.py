"""OCTO-38 — a thread row says which surface it is, and only work can carry a project.

`Thread.kind` records the surface when the row is created; `ThreadManager.ensure`
refuses a project for a DM or a group chat rather than dropping it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.managers import ThreadManager, UserManager
from octomate.schemas.project import Project
from octomate.schemas.thread import (
    CLAUDE_NATIVE_ID,
    CODEX_NATIVE_ID,
    Thread,
    ThreadKey,
)
from tests.support.managers import a_project, a_registry

SLACK_DM = ThreadKey("slack", "dm", "D1")
SLACK_CHANNEL = ThreadKey("slack", "group", "C1")
SLACK_THREAD = ThreadKey("slack", "thread", "C1", "1710000000.000001")
LARK_SUB_THREAD = ThreadKey("lark", "thread", "oc_1", "omt_sub_1")
CLAUDE_SESSION = ThreadKey(CLAUDE_NATIVE_ID, "thread", "sess-1")
CODEX_SESSION = ThreadKey(CODEX_NATIVE_ID, "thread", "sess-2")


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


async def inky(tmp_path: Path) -> Project:
    """The one project a test attributes to. Its row has to be persisted:
    `threads.project_id` is a real foreign key."""
    root = tmp_path / "inky"
    root.mkdir(parents=True, exist_ok=True)
    manager = await a_registry(a_project(root))
    project = manager.get("inky")
    assert project is not None
    return project


async def test_each_surface_is_recorded_as_what_it_is() -> None:
    # A native thread's key has a DM's shape; only its tentacle separates them.
    manager = ThreadManager(users=UserManager())

    kinds = [
        (await manager.ensure(key)).kind
        for key in (
            SLACK_DM,
            SLACK_CHANNEL,
            SLACK_THREAD,
            LARK_SUB_THREAD,
            CLAUDE_SESSION,
            CODEX_SESSION,
        )
    ]

    assert kinds == [
        "dm",
        "group",
        "thread",
        "thread",
        "native_thread",
        "native_thread",
    ]


async def test_the_kind_is_on_the_row_a_later_reader_loads() -> None:
    """The point of the column: a later reader takes the kind off the row."""
    await ThreadManager(users=UserManager()).ensure(SLACK_DM)

    reloaded = await ThreadManager(users=UserManager()).ensure(SLACK_DM)

    assert reloaded.kind == "dm"


def test_a_row_that_contradicts_its_key_is_refused() -> None:
    """The key is the channel's fact and the row is our copy, so a copy that
    disagrees is corrupt rather than a second opinion."""
    with pytest.raises(ValidationError, match="calls itself a thread"):
        Thread(
            channel_tentacle_id="slack",
            chat_type="dm",
            chat_id="D1",
            kind="thread",
        )


def test_a_native_row_cannot_call_itself_a_plain_thread() -> None:
    """The slip the fourth kind exists to catch: a native thread and a Slack thread
    share a `chat_type`, and only the tentacle tells them apart."""
    with pytest.raises(ValidationError, match="is a native_thread"):
        Thread(
            channel_tentacle_id=CLAUDE_NATIVE_ID,
            chat_type="thread",
            chat_id="sess-1",
            kind="thread",
        )


async def test_a_dm_is_refused_a_project(tmp_path: Path) -> None:
    manager = ThreadManager(users=UserManager())
    project = await inky(tmp_path)

    with pytest.raises(ValueError, match="is a dm and cannot be attributed"):
        await manager.ensure(SLACK_DM, project=project)

    # Refused, not dropped — and no row written behind the caller's back.
    assert (await manager.ensure(SLACK_DM)).project_id is None


async def test_a_group_chat_is_refused_a_project(tmp_path: Path) -> None:
    manager = ThreadManager(users=UserManager())
    project = await inky(tmp_path)

    with pytest.raises(ValueError, match="is a group and cannot be attributed"):
        await manager.ensure(SLACK_CHANNEL, project=project)


async def test_a_dm_that_already_exists_is_refused_too(tmp_path: Path) -> None:
    """`ensure` ignores `project` for an existing thread, so the refusal must not
    depend on which call came first."""
    manager = ThreadManager(users=UserManager())
    project = await inky(tmp_path)
    await manager.ensure(SLACK_DM)

    with pytest.raises(ValueError, match="is a dm and cannot be attributed"):
        await manager.ensure(SLACK_DM, project=project)


async def test_a_slack_thread_and_a_lark_sub_thread_are_attributed(
    tmp_path: Path,
) -> None:
    manager = ThreadManager(users=UserManager())
    project = await inky(tmp_path)

    slack = await manager.ensure(SLACK_THREAD, project=project)
    lark = await manager.ensure(LARK_SUB_THREAD, project=project)

    assert [slack.project_id, lark.project_id] == [project.id, project.id]


async def test_a_native_thread_keeps_the_attribution_octo_31_gives_it(
    tmp_path: Path,
) -> None:
    manager = ThreadManager(users=UserManager())
    project = await inky(tmp_path)

    claude = await manager.ensure(CLAUDE_SESSION, project=project)
    codex = await manager.ensure(CODEX_SESSION, project=project)

    assert [claude.project_id, codex.project_id] == [project.id, project.id]
