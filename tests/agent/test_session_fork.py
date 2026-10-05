"""Runtime-specific requirements for forking a conversation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.schemas.conversation import Conversation
from octomate.tentacles.inkling import InklingTentacle


async def test_inkling_fork_needs_no_external_session(tmp_path: Path) -> None:
    tentacle = InklingTentacle("inkling", Octomate(), models={"test": TestModel()})
    conversation = Conversation(thread_id=uuid7(), agent_tentacle_id="inkling")

    assert await tentacle.fork_session(conversation, cwd=tmp_path) is None

    conversation.external_id = "unexpected-runtime"
    with pytest.raises(ValueError, match="cannot have an external session id"):
        await tentacle.fork_session(conversation, cwd=tmp_path)
