"""Opt-in, keyless integration against a built dsh checkout.

DSH_TEST_EXECUTABLE=/path/to/apps/cli/lib/bin.js pytest tests/agent/test_deepseek_live.py
All settings, sessions, and tool execution are confined to pytest's temporary tree.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from octomate_cli.streaming.deepseek import DshHistoryClient, new_entries
from pydantic import HttpUrl
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import FunctionToolResultEvent, PartStartEvent
from websockets.asyncio.client import connect
from websockets.typing import Origin

from octomate import Octomate
from octomate.config.agents import DeepseekConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.tentacles.deepseek import DeepseekTentacle
from octomate.tentacles.deepseek.base import DeepseekBridgeContext
from octomate.tentacles.deepseek.client import DeepseekApiClient
from octomate.tentacles.deepseek.process import DeepseekProcess
from octomate.tentacles.deepseek.wire import (
    ApprovalRequestedFrame,
    ErrResult,
    OkResult,
    QuestionRequestedFrame,
)
from tests.support.managers import FakeConversationManager


async def test_real_harness_drives_resumes_and_reads_native_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = os.environ.get("DSH_TEST_EXECUTABLE")
    if executable is None:
        pytest.skip("set DSH_TEST_EXECUTABLE to run the isolated dsh integration")
    root = (await asyncio.to_thread(Path(executable).resolve)).parents[3]
    for key in list(os.environ):
        if key.startswith(("DSH_", "DEEPSEEK_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("DSH_AGENTS_HOME", str(tmp_path / ".agents"))
    monkeypatch.setenv("DSH_TELEMETRY_DISABLED", "1")
    monkeypatch.setenv("NODE_NO_WARNINGS", "1")
    home = tmp_path / ".dsh"
    home.mkdir()
    (home / "settings.yaml").write_text(
        "agent-default-model:\n  provider: octomate-test\n  model: mock\n"
    )
    credentials = home / ".credentials.yaml"
    credentials.write_text("version: 1\nrefs:\n  OCTOMATE_DSH_TEST_TOKEN: synthetic\n")
    credentials.chmod(0o600)
    native_patch = "- insert:\n    - name: native-extension-must-not-load\n"
    (home / "cordis.patch.yml").write_text(native_patch)
    native_profile = home / "profiles" / "web"
    native_profile.mkdir(parents=True)
    (native_profile / "cordis.patch.yml").write_text(native_patch)
    plugin = tmp_path / "mock.mjs"
    plugin.write_text(
        (Path(__file__).parent / "fixtures/dsh_remote_mock.mjs")
        .read_text()
        .replace(
            "__DSH_LLM_MODULE__", (root / "packages/llm/llm/lib/index.js").as_uri()
        )
    )
    patch = tmp_path / "patch.yml"
    patch.write_text(f"- insert:\n    - id: octomate-smoke\n      name: {plugin}\n")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    conversations = FakeConversationManager()
    tentacle = DeepseekTentacle(
        "deepseek",
        Octomate(conversations=conversations),
        config=DeepseekConfig(
            executable=executable,
            port=port,
            dsh_home=home,
            extra_args=["--patch", str(patch)],
        ),
    )
    approvals: list[ApprovalRequestedFrame] = []
    questions: list[QuestionRequestedFrame] = []

    async def approve(
        context: DeepseekBridgeContext, frame: ApprovalRequestedFrame
    ) -> OkResult:
        approvals.append(frame)
        return OkResult(value="allowed-once")

    async def answer(
        context: DeepseekBridgeContext, frame: QuestionRequestedFrame
    ) -> OkResult:
        questions.append(frame)
        return OkResult(
            value={
                "answers": [
                    {"id": frame.questions[0].id, "selected": [], "custom": "continue"}
                ]
            }
        )

    monkeypatch.setattr(tentacle, "answer_approval", approve)
    monkeypatch.setattr(tentacle, "answer_questions", answer)
    address = ChannelAddress(
        channel_tentacle_id="im", chat_type="dm", chat_id="smoke", user_id="smoke"
    )
    thread = uuid.uuid4()
    async with asyncio.timeout(90), tentacle:
        async with tentacle.run_stream_events(
            "Test integration",
            conversation_address=address,
            thread_id=thread,
            model="octomate-test:mock",
        ) as stream:
            events = [event async for event in stream]
        assert isinstance(events[-1], AgentRunResultEvent)
        assert events[-1].result.output == "Octomate Remote API works"
        assert any(isinstance(event, FunctionToolResultEvent) for event in events)
        assert any(isinstance(event, PartStartEvent) for event in events)
        assert events[-1].result.usage.output_tokens == 6
        first_session = conversations.runs[0][0].external_id
        assert first_session is not None
        resumed = await tentacle.run(
            "Continue",
            conversation_address=address,
            thread_id=thread,
            model="octomate-test:mock",
        )
        assert resumed.output == "Octomate Remote API works"
        assert conversations.runs[-1][0].external_id == first_session
        assert len(conversations.runs) == 2
        assert len(approvals) == 2
        assert len(questions) == 2
        assert all(frame.session_id == first_session for frame in approvals + questions)
        assert tentacle.process is not None
        assert tentacle.process.launch_token is not None
        monkeypatch.setenv(
            "DSH_LAUNCH_TOKEN", tentacle.process.launch_token.get_secret_value()
        )
        history = await asyncio.to_thread(
            DshHistoryClient, str(tentacle.client.base_url)
        )
        records = await asyncio.to_thread(new_entries, history, first_session, 0)
        kinds = [
            event["type"]
            for entry in records
            if isinstance(event := entry.get("event"), dict)
        ]
        assert kinds.count("turn/end") == 2
        assert "tool/result" in kinds
        reader = DeepseekProcess(
            executable=executable,
            port=0,
            extra_args=["--patch", str(patch)],
            dsh_home=home,
            ready_timeout=60,
        )
        try:
            reader_url = await reader.start()
            assert reader.launch_token is not None
            async with DeepseekApiClient(
                reader_url, httpx.AsyncClient(base_url=str(reader_url), trust_env=False)
            ) as reader_client:
                await reader_client.authenticate(reader.launch_token)
                with monkeypatch.context() as reader_env:
                    reader_env.setenv(
                        "DSH_LAUNCH_TOKEN", reader.launch_token.get_secret_value()
                    )
                    shared_history = await asyncio.to_thread(
                        DshHistoryClient, str(reader_url)
                    )
                    shared_records = await asyncio.to_thread(
                        new_entries, shared_history, first_session, 0
                    )
                    assert shared_records == records
                busy = await reader_client.remote(
                    "session/create",
                    {
                        "request": {
                            "sessionId": first_session,
                            "cwd": str(
                                tentacle.octomate.workspaces.open(thread, None).path
                            ),
                        }
                    },
                )
                assert isinstance(busy, ErrResult)
                assert "already owned" in busy.error.message
        finally:
            await reader.stop()
        waiting = asyncio.Event()
        cancelled = asyncio.Event()

        async def wait_for_cancel(
            context: DeepseekBridgeContext, frame: QuestionRequestedFrame
        ) -> OkResult:
            waiting.set()
            try:
                await asyncio.Future[None]()
            finally:
                cancelled.set()
            raise AssertionError("The pending question should have been cancelled")

        monkeypatch.setattr(tentacle, "answer_questions", wait_for_cancel)
        running = asyncio.create_task(
            tentacle.run(
                "Cancel this turn", conversation_address=address, thread_id=thread
            )
        )
        await asyncio.wait_for(waiting.wait(), 10)
        reply = await tentacle.client.remote(
            "session/cancel", {"request": {"sessionId": first_session}}
        )
        assert isinstance(reply, OkResult)
        await asyncio.wait_for(running, 10)
        await asyncio.wait_for(cancelled.wait(), 10)
        assert not tentacle.subscribers
        assert not tentacle.bridge_contexts
    assert tentacle.process is None
    assert (home / "cordis.patch.yml").read_text() == native_patch
    assert (native_profile / "cordis.patch.yml").read_text() == native_patch
    assert "OCTOMATE_DSH_TEST_TOKEN: synthetic\n" in credentials.read_text()
    restarted = DeepseekTentacle("deepseek", tentacle.octomate, config=tentacle.config)
    monkeypatch.setattr(restarted, "answer_approval", approve)
    monkeypatch.setattr(restarted, "answer_questions", answer)
    async with asyncio.timeout(90), restarted:
        result = await restarted.run(
            "Continue after restart", conversation_address=address, thread_id=thread
        )
        assert result.output == "Octomate Remote API works"
        assert conversations.runs[-1][0].external_id == first_session


async def test_real_harness_browser_login_through_a_trusted_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    executable = os.environ.get("DSH_TEST_EXECUTABLE")
    if executable is None:
        pytest.skip("set DSH_TEST_EXECUTABLE to run the isolated dsh integration")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DSH_TELEMETRY_DISABLED", "1")
    for key in tuple(os.environ):
        if key.endswith("_API_KEY"):
            monkeypatch.delenv(key)
    authority = "dsh.example:8443"
    origin = f"https://{authority}"
    dsh = DeepseekProcess(
        executable=executable,
        port=0,
        extra_args=[],
        dsh_home=tmp_path / ".dsh",
        ready_timeout=60,
        browser_url=HttpUrl(origin),
    )
    caplog.set_level(logging.INFO, logger="octomate.tentacles.deepseek.process")
    try:
        base_url = await dsh.start()
        banners = [
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("dsh web: ")
        ]
        assert len(banners) == 1
        link = urlsplit(banners[0].removeprefix("dsh web: "))
        assert link.netloc == authority
        # Model the proxy's loopback connection while preserving browser Host/Origin.
        async with httpx.AsyncClient(
            base_url=str(base_url),
            headers={"Host": authority, "Origin": origin},
            trust_env=False,
        ) as browser:
            assert (await browser.get("/")).status_code == 401
            login = await browser.get(f"/?{link.query}")
            assert login.status_code == 303
            assert login.headers["location"] == "/"
            assert (await browser.get("/")).status_code == 200
            request = {
                "type": "client-request",
                "rpcId": "browser-probe",
                "method": "settings/describe",
                "payload": {"args": {}},
            }
            assert (
                await browser.post("/api/settings/describe", json=request)
            ).status_code == 200
            rejected = await browser.post(
                "/api/settings/describe",
                json=request,
                headers={
                    "Host": "untrusted.example",
                    "Origin": "https://untrusted.example",
                },
            )
            assert rejected.status_code == 403
            cookie = login.headers["set-cookie"].split(";", 1)[0]
            async with connect(
                f"ws://{authority}/api/remote.mux",
                host="127.0.0.1",
                port=base_url.port,
                origin=Origin(origin),
                additional_headers={"Cookie": cookie},
                proxy=None,
            ) as socket:
                await socket.send(
                    json.dumps(
                        {
                            "type": "open",
                            "streamId": "$events",
                            "endpoint": "$events",
                            "payload": {"args": {}},
                        }
                    )
                )
                ready = json.loads(await asyncio.wait_for(socket.recv(), 10))
                assert ready["value"]["clientId"]
    finally:
        await dsh.stop()
