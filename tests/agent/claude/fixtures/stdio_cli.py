"""A scripted Claude CLI peer for the real SDK's duplex control protocol."""

import json
import os
import sys
from pathlib import Path

from pydantic import JsonValue
from uuid_utils.compat import uuid7


def send(message: dict[str, JsonValue]) -> None:
    print(json.dumps(message), flush=True)


def finish(text: str) -> None:
    send(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "scripted",
                "content": [{"type": "text", "text": text}],
            },
            "parent_tool_use_id": None,
        }
    )
    send(
        {
            "type": "result",
            "subtype": "success",
            "duration_ms": 1,
            "duration_api_ms": 1,
            "is_error": False,
            "num_turns": 1,
            "session_id": "scripted-session",
            "result": text,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )


with Path(os.environ["CLAUDE_TEST_LOG"]).open("w") as log:
    log.write(json.dumps({"pid": os.getpid(), "argv": sys.argv}) + "\n")
    log.flush()
    for line in sys.stdin:
        message = json.loads(line)
        log.write(json.dumps(message) + "\n")
        log.flush()
        if message["type"] == "control_request":
            subtype = message["request"]["subtype"]
            if subtype == "initialize" and os.getenv("CLAUDE_TEST_FAIL_INIT"):
                sys.exit(9)
            send(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": message["request_id"],
                        "response": {"commands": [], "models": []},
                    },
                }
            )
            if subtype == "interrupt":
                finish("Interrupted")
        elif message["type"] == "user":
            send(
                {
                    "type": "stream_event",
                    "uuid": str(uuid7()),
                    "session_id": "scripted-session",
                    "parent_tool_use_id": None,
                    "event": {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                }
            )
            send(
                {
                    "type": "stream_event",
                    "uuid": str(uuid7()),
                    "session_id": "scripted-session",
                    "parent_tool_use_id": None,
                    "event": {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "Working"},
                    },
                }
            )
            send(
                {
                    "type": "control_request",
                    "request_id": "approval",
                    "request": {
                        "subtype": "can_use_tool",
                        "tool_name": "Bash",
                        "tool_use_id": "tool-1",
                        "input": {"command": "printf approved"},
                    },
                }
            )
        elif message["type"] == "control_response":
            finish("Approved")
