"""Native clients attach only explicitly referenced, workspace-owned HTML."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from octomate_cli.streaming.codex import CodexSessionTail
from octomate_cli.streaming.files import SessionTail
from octomate_protocol.stream import StreamLine
from octomate_protocol.visualizations import HtmlSnapshot
from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection


@pytest.mark.parametrize("tail_type", [SessionTail, CodexSessionTail])
async def test_native_frames_keep_raw_lines_and_attach_unique_snapshots(
    tmp_path: Path, tail_type: type[SessionTail]
) -> None:
    source = tmp_path / "counter.html"
    source.write_text("<button>上海 ✓</button>")
    reference = {"path": str(source), "title": "Counter", "mode": "wide"}
    marker = "visualize" + json.dumps(reference) + ""
    raw = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": f"{marker}\n{marker}"}],
            },
        }
    )
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_bytes(raw.encode() + b"\n")
    tail = tail_type("native", transcript, cwd=str(tmp_path))
    socket = AsyncMock(spec=ClientConnection)

    assert await tail.pump(socket)
    [call] = socket.send.call_args_list
    frame = StreamLine.model_validate_json(call.args[0])
    assert frame.line == raw
    assert frame.start == 0
    assert frame.end == len(raw.encode()) + 1
    assert len(frame.visualizations) == int(tail_type is CodexSessionTail)
    if tail_type is CodexSessionTail:
        [snapshot] = frame.visualizations
        assert snapshot.reference.model_dump(mode="json") == reference
        assert snapshot.html == "<button>上海 ✓</button>"


@pytest.mark.parametrize("key", ["", "rollout-child.jsonl"])
async def test_capture_needs_the_root_rollout_and_a_workspace(
    tmp_path: Path, key: str
) -> None:
    raw = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"text": 'visualize{"path":"/private/missing.html"}'}],
            },
        }
    ).encode()
    tail = CodexSessionTail(
        "native", tmp_path / "rollout.jsonl", cwd=str(tmp_path) if key else ""
    )
    frame = await tail.frame(key, 0, len(raw) + 1, raw)
    assert frame.agent_id == (key or None)
    assert frame.line == raw.decode()
    assert frame.visualizations == []


@pytest.mark.parametrize(
    ("kind", "role"),
    [
        ("message", "user"),
        ("function_call_output", "assistant"),
    ],
)
async def test_prompts_and_tool_output_cannot_trigger_file_reads(
    tmp_path: Path, kind: str, role: str
) -> None:
    marker = 'visualize{"path":"/private/missing.html"}'
    raw = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": kind,
                "role": role,
                "content": [{"text": marker}],
            },
        }
    )
    tail = CodexSessionTail("native", tmp_path / "rollout.jsonl", cwd=str(tmp_path))
    frame = await tail.frame("", 0, len(raw.encode()) + 1, raw.encode())
    assert frame.visualizations == []


@pytest.mark.parametrize("invalid", ["outside", "symlink", "missing", "large"])
async def test_native_capture_uses_the_same_bounded_file_checks(
    tmp_path: Path, invalid: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "counter.html"
    outside = tmp_path / "private.html"
    outside.write_text("private")
    if invalid == "outside":
        source = outside
    elif invalid == "symlink":
        source.symlink_to(outside)
    elif invalid == "large":
        source.write_bytes(b"x" * 1_000_001)
    marker = "visualize" + json.dumps({"path": str(source)}) + ""
    raw = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"text": marker}],
            },
        }
    )
    tail = CodexSessionTail("native", tmp_path / "rollout.jsonl", cwd=str(workspace))
    with pytest.raises((ValueError, OSError)):
        await tail.frame("", 0, len(raw.encode()) + 1, raw.encode())


@pytest.mark.parametrize("html", [" \n", "界" * 333_334])
def test_snapshot_validation_checks_utf8_bytes_on_the_server(html: str) -> None:
    with pytest.raises(ValidationError):
        HtmlSnapshot.model_validate(
            {
                "reference": {"path": "/client/counter.html"},
                "html": html,
            }
        )


@pytest.mark.parametrize("wrong", ["path", "title", "user", "subagent"])
def test_stream_rejects_snapshots_not_advertised_by_the_root_assistant(
    wrong: str,
) -> None:
    reference = {"path": "/client/counter.html", "title": "Counter"}
    marker = "visualize" + json.dumps(reference) + ""
    raw = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user" if wrong == "user" else "assistant",
                "content": [{"text": marker}],
            },
        }
    )
    snapshot_reference = dict(reference)
    if wrong == "path":
        snapshot_reference["path"] = "/client/another.html"
    elif wrong == "title":
        snapshot_reference["title"] = "Other"
    with pytest.raises(ValidationError, match="root assistant references"):
        StreamLine.model_validate(
            {
                "start": 0,
                "end": len(raw.encode()) + 1,
                "line": raw,
                "agent_id": "child" if wrong == "subagent" else None,
                "visualizations": [
                    {"reference": snapshot_reference, "html": "<p>Test</p>"}
                ],
            }
        )
