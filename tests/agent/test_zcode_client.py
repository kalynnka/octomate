from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path

import anyio
import pytest
from pydantic import SecretStr
from pydantic_ai.exceptions import AgentRunError

from octomate.tentacles.zcode.client import ZcodeClient
from octomate.tentacles.zcode.wire import (
    InteractionRequest,
    StateUpdated,
    TurnStartedEvent,
)
from octomate.types.json import JsonObject
from tests.support.zcode import permission_request, question_request


def client_for(script: str, path: Path, *, timeout: float = 2) -> ZcodeClient:
    return ZcodeClient(
        [sys.executable, "-u", "-c", script],
        cwd=path,
        state_dir=path / "state",
        request_timeout=timeout,
        secrets=[SecretStr("secret-test-key")],
    )


async def test_callbacks_interleave_with_pending_rpc_and_use_distinct_ids(
    tmp_path: Path,
) -> None:
    script = f"""
import json, sys, os
request = json.loads(sys.stdin.readline())
permission = json.loads({permission_request().model_dump_json(by_alias=True)!r})
question = json.loads({question_request().model_dump_json(by_alias=True)!r})
callbacks = [{{'method': 'session/requestRuntimePreferences'}}, permission, question, {{'method': 'unknown/request'}}]
answers = []
for callback in callbacks:
    print(json.dumps({{'id': 1, **callback}}), flush=True)
    answers.append(json.loads(sys.stdin.readline()))
print(json.dumps({{'id': request['id'], 'result': {{'answers': answers, 'db': os.environ['ZCODE_SESSION_DB_PATH']}}}}), flush=True)
sys.stdin.read()
"""
    async with client_for(script, tmp_path) as client:
        result = await client.call("test", {})
        assert isinstance(result, dict)
        answers = result["answers"]
        assert isinstance(answers, list)
        assert answers[0] == {
            "id": 1,
            "result": {
                "nativeSearchEnhancementsEnabled": False,
                "memoryEnabled": False,
                "askUserQuestionAutoResolutionEnabled": False,
                "modelContextBudgetStrategy": "preflight-v1",
            },
        }
        assert isinstance(answers[1], dict)
        approval = answers[1]["result"]
        assert isinstance(approval, dict)
        assert approval["decision"] == "deny"
        assert isinstance(answers[2], dict)
        question = answers[2]["result"]
        assert isinstance(question, dict)
        assert question["action"] == "decline"
        assert isinstance(answers[3], dict)
        error = answers[3]["error"]
        assert isinstance(error, dict)
        assert error["code"] == -32601
        assert result["db"] == str(tmp_path / "state" / "sessions.db")
    assert client.process is not None
    assert client.process.returncode is not None


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("print('not-json', flush=True)", "invalid protocol"),
        ("print('{\"id\":1}', flush=True)", "invalid RPC response"),
        ("pass", "closed unexpectedly"),
        (
            "import json,sys; q=json.loads(sys.stdin.readline()); print(json.dumps({'id':q['id'],'error':{'code':-1,'message':'secret-test-key denied'}}),flush=True); sys.stdin.read()",
            "redacted",
        ),
    ],
)
async def test_transport_failures_are_clear_and_redacted(
    tmp_path: Path, script: str, message: str
) -> None:
    async with client_for(script, tmp_path) as client:
        with pytest.raises(AgentRunError, match=message) as error:
            await client.call("test", {})
        assert "secret-test-key" not in str(error.value)


async def test_disconnect_wakes_both_rpc_and_event_consumers(tmp_path: Path) -> None:
    script = "import sys; sys.stdin.readline()"
    async with client_for(script, tmp_path) as client:
        with pytest.raises(AgentRunError, match="closed"):
            await client.call("test", {})
        assert isinstance(
            await asyncio.wait_for(client.events.get(), timeout=1), AgentRunError
        )
        assert not client.pending


async def test_timeout_and_cancelled_rpc_remove_waiters(tmp_path: Path) -> None:
    async with client_for(
        "import sys; sys.stdin.read()", tmp_path, timeout=0.1
    ) as client:
        with pytest.raises(AgentRunError, match="timed out"):
            await client.call("test", {})
        assert not client.pending
        waiting = asyncio.create_task(client.call("test", {}))
        await asyncio.sleep(0)
        assert client.pending
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not client.pending


async def test_cancel_scope_still_reaps_the_child(tmp_path: Path) -> None:
    client = client_for("import sys; sys.stdin.read()", tmp_path)
    started = asyncio.Event()

    async def run() -> None:
        async with client:
            started.set()
            await anyio.sleep_forever()

    async with anyio.create_task_group() as group:
        group.start_soon(run)
        await started.wait()
        group.cancel_scope.cancel()
    assert client.process is not None
    assert client.process.returncode is not None


async def test_desktop_auth_callback_fails_explicitly(tmp_path: Path) -> None:
    script = """
import sys, json
sys.stdin.readline()
print(json.dumps({'id':'auth','method':'interaction/requestProviderRuntimeHeaders','params':{}}),flush=True)
sys.stdin.read()
"""
    async with client_for(script, tmp_path) as client:
        with pytest.raises(AgentRunError, match="desktop-login"):
            await client.call("session/create", {})


async def test_waiting_interaction_deduplicates_without_blocking_rpc_or_events(
    tmp_path: Path,
) -> None:
    script = f"""
import json, sys
callback = json.loads({permission_request().model_dump_json(by_alias=True)!r})
request = json.loads(sys.stdin.readline())
for rpc_id in ['first', 'repeat']:
    print(json.dumps({{'id': rpc_id, **callback}}), flush=True)
print(json.dumps({{'id': 'prefs', 'method': 'session/requestRuntimePreferences'}}), flush=True)
print(json.dumps({{'id': request['id'], 'result': 'started'}}), flush=True)
answers = []
for line in sys.stdin:
    frame = json.loads(line)
    if frame.get('method') == 'ping':
        print(json.dumps({{'id': frame['id'], 'result': 'pong'}}), flush=True)
    elif frame.get('method') == 'repeat':
        print(json.dumps({{'id': 'after-resolution', **callback}}), flush=True)
        request = frame
    elif frame.get('id') == 'prefs':
        print(json.dumps({{'method': 'session/event', 'params': {{'sessionId': 'session-1', 'eventId': 'prefs-answered', 'seq': 1, 'turnId': 'turn-1', 'type': 'turn.started', 'payload': {{'inputId': 'input-1', 'messageId': 'message-1'}}}}}}), flush=True)
    else:
        answers.append(frame['result'])
        if len(answers) == 3:
            print(json.dumps({{'id': request['id'], 'result': answers}}), flush=True)
"""
    calls: list[InteractionRequest] = []
    started, release = asyncio.Event(), asyncio.Event()

    async def answer(request: InteractionRequest) -> JsonObject:
        calls.append(request)
        started.set()
        await release.wait()
        return {"decision": "allow"}

    client = client_for(script, tmp_path)
    client.interaction_handler = answer
    async with client:
        assert await client.call("start", {}) == "started"
        await asyncio.wait_for(started.wait(), 1)
        assert await client.call("ping", {}) == "pong"
        event = await asyncio.wait_for(client.events.get(), 1)
        assert isinstance(event, TurnStartedEvent)
        assert event.payload.input_id == "input-1"
        assert len(calls) == 1
        release.set()
        assert await client.call("repeat", {}) == [{"decision": "allow"}] * 3
        assert len(calls) == 1


@pytest.mark.parametrize("failure", ["malformed", "conflicting", "handler"])
async def test_invalid_interactions_fail_clearly(tmp_path: Path, failure: str) -> None:
    script = f"""
import json, sys
sys.stdin.readline()
callback = json.loads({permission_request().model_dump_json(by_alias=True)!r})
if {failure!r} == 'malformed':
    callback['params']['riskLevel'] = {{'secret': 'secret-test-key'}}
print(json.dumps({{'id': 'first', **callback}}), flush=True)
if {failure!r} == 'conflicting':
    callback['params']['input'] = {{'command': 'different'}}
    print(json.dumps({{'id': 'second', **callback}}), flush=True)
sys.stdin.read()
"""

    async def answer(request: InteractionRequest) -> JsonObject:
        if failure == "handler":
            raise RuntimeError("secret-test-key callback failed")
        await asyncio.Event().wait()
        return {"decision": "deny"}

    client = client_for(script, tmp_path)
    client.interaction_handler = answer
    async with client:
        with pytest.raises(AgentRunError) as error:
            await client.call("start", {})
        assert "secret-test-key" not in str(error.value)
        assert "interaction" in str(error.value)
    assert not client.interactions
    assert not client.callback_tasks


@pytest.mark.parametrize("parent_exits", [True, False])
async def test_cleanup_kills_children_even_after_parent_exit(
    tmp_path: Path, parent_exits: bool
) -> None:
    script = f"""
import json, os, signal, subprocess, sys
request = json.loads(sys.stdin.readline())
child = subprocess.Popen([sys.executable, '-u', '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready", flush=True); time.sleep(60)'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
assert child.stdout.readline() == b'ready\\n'
print(json.dumps({{'id': request['id'], 'result': child.pid}}), flush=True)
if not {parent_exits!r}:
    sys.stdin.read()
"""
    client = client_for(script, tmp_path)
    child_pid: int | None = None
    try:
        async with client:
            result = await client.call("spawn", {})
            assert isinstance(result, int)
            child_pid = result
            assert client.process is not None
            if parent_exits:
                await asyncio.wait_for(client.process.wait(), 2)
        status = await asyncio.create_subprocess_exec(
            "ps", "-o", "stat=", "-p", str(child_pid), stdout=asyncio.subprocess.PIPE
        )
        stdout, _ = await status.communicate()
        assert not stdout.strip() or stdout.strip().startswith(b"Z")
    finally:
        if child_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)


async def test_oversized_protocol_frame_is_reported_explicitly(tmp_path: Path) -> None:
    script = "import json,sys; q=json.loads(sys.stdin.readline()); print(json.dumps({'id':q['id'],'result':'x'*(17*1024*1024)}),flush=True); sys.stdin.read()"
    async with client_for(script, tmp_path) as client:
        with pytest.raises(AgentRunError, match="exceeds the 16 MiB limit"):
            await client.call("session/messages", {})


async def test_state_failures_and_typed_events_reach_consumers(tmp_path: Path) -> None:
    script = """
import json, sys
q = json.loads(sys.stdin.readline())
for frame in [
    {'method':'workspace/unrelated', 'params':{}},
    {'method':'session/event', 'params':{'type':'session.updated'}},
    {'id':q['id'], 'result':{'accepted':True, 'sessionId':'s', 'stateRevision':2}},
    {'method':'state.updated', 'params':{'type':'state.updated', 'scope':'session', 'sessionId':'s', 'revision':3, 'reason':'prompt_failed', 'patch':{'status':'idle'}}},
]:
    print(json.dumps(frame), flush=True)
sys.stdin.read()
"""
    async with client_for(script, tmp_path) as client:
        await client.call("session/send", {})
        update = await asyncio.wait_for(client.events.get(), 1)
        assert isinstance(update, StateUpdated)
        assert update.reason == "prompt_failed"
        assert update.revision == 3
        assert client.events.empty()


async def test_malformed_supported_event_fails_the_transport(tmp_path: Path) -> None:
    script = """
import json, sys
sys.stdin.readline()
print(json.dumps({'method':'session/event','params':{'type':'model.streaming','sessionId':'s','turnId':'t','seq':1,'eventId':'e','payload':{'kind':'text_delta'}}}), flush=True)
sys.stdin.read()
"""
    async with client_for(script, tmp_path) as client:
        with pytest.raises(AgentRunError, match="invalid protocol frame"):
            await client.call("session/send", {})
