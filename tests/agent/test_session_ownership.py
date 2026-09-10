import asyncio

import pytest

from octomate import Octomate
from octomate.config import ClaudeCodeConfig, CodexConfig
from octomate.tentacles.claude import ClaudeCodeTentacle
from octomate.tentacles.codex import CodexTentacle
from tests.support.agents import FakeAgent


async def test_session_ownership_is_scoped_to_the_runtime_and_host() -> None:
    octomate = Octomate()
    driver = octomate.connect(
        ClaudeCodeTentacle("configured-claude", octomate, config=ClaudeCodeConfig())
    )
    receiver = octomate.connect(
        ClaudeCodeTentacle("other-claude", octomate, config=ClaudeCodeConfig())
    )
    codex = octomate.connect(CodexTentacle("codex", octomate, config=CodexConfig()))
    other = Octomate()
    other_driver = other.connect(
        ClaudeCodeTentacle("configured-claude", other, config=ClaudeCodeConfig())
    )

    async with driver.driving("session"):
        assert not driver.should_ingest_session("session")
        assert not receiver.should_ingest_session("session")
        assert receiver.should_ingest_session("other-session")
        assert codex.should_ingest_session("session")
        assert other_driver.should_ingest_session("session")

    assert receiver.should_ingest_session("session")


async def test_unregistered_agents_keep_overlapping_claims_until_the_last_release() -> (
    None
):
    driver = ClaudeCodeTentacle("claude", Octomate(), config=ClaudeCodeConfig())

    async def failed_run() -> None:
        async with driver.driving("session"):
            assert driver.driven_sessions == {"session": 2}
            raise RuntimeError("run failed")

    async with driver.driving("session"):
        assert driver.driven_sessions == {"session": 1}
        with pytest.raises(RuntimeError, match="run failed"):
            await failed_run()
        assert driver.driven_sessions == {"session": 1}
        assert not driver.should_ingest_session("session")

    assert driver.driven_sessions == {}
    assert driver.should_ingest_session("session")


async def test_every_agent_has_independent_driven_and_native_counters() -> None:
    octomate = Octomate()
    agent = octomate.connect(FakeAgent())
    other = octomate.connect(FakeAgent(id="other"))

    assert agent.native_id is None
    assert agent.driven_sessions == {}
    assert agent.native_sessions == {}
    async with agent.driving("session", native=True):
        assert agent.native_sessions == {"session": 1}
        assert agent.driven_sessions == {}
        assert agent.should_ingest_session("session")
        async with agent.driving("session"):
            assert agent.driven_sessions == {"session": 1}
            assert agent.native_sessions == {"session": 1}
            assert not agent.should_ingest_session("session")
            assert other.driven_sessions == {}
            assert other.native_sessions == {}
            assert other.should_ingest_session("session")
        assert agent.driven_sessions == {}

    assert agent.native_sessions == {}
    assert agent.should_ingest_session("session")


async def test_native_counts_hold_overlapping_streams_until_the_last_release() -> None:
    agent = FakeAgent()

    async def failed_stream() -> None:
        async with agent.driving("session", native=True):
            assert agent.native_sessions == {"session": 2}
            raise RuntimeError("stream failed")

    async with agent.driving("session", native=True):
        with pytest.raises(RuntimeError, match="stream failed"):
            await failed_stream()
        assert agent.native_sessions == {"session": 1}

    assert agent.native_sessions == {}


@pytest.mark.parametrize("native", [False, True], ids=["driven", "native"])
async def test_cancelled_task_releases_only_its_own_claim(native: bool) -> None:
    agent = FakeAgent()
    sessions = agent.native_sessions if native else agent.driven_sessions
    entered = asyncio.Event()
    release = asyncio.Event()

    async def wait_in_session() -> None:
        async with agent.driving("session", native=native):
            entered.set()
            await release.wait()

    async with agent.driving("session", native=native):
        task = asyncio.create_task(wait_in_session())
        await entered.wait()
        assert sessions == {"session": 2}
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sessions == {"session": 1}

    assert sessions == {}
