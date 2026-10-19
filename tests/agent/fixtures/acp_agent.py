"""Scripted stdio peer for native ACP client tests; never starts a real agent."""

import json
import os
import sys
from pathlib import Path

from pydantic import JsonValue


def send(message: dict[str, JsonValue]) -> None:
    print(json.dumps({"jsonrpc": "2.0", **message}), flush=True)


def main() -> None:
    pending: str | int | None = None
    session_count = 0
    with Path(os.environ["ACP_TEST_LOG"]).open("w") as log:
        for line in sys.stdin:
            message = json.loads(line)
            log.write(json.dumps({"pid": os.getpid(), "message": message}) + "\n")
            log.flush()
            match message.get("method"):
                case "initialize":
                    send(
                        {
                            "id": message["id"],
                            "result": {
                                "protocolVersion": int(
                                    os.getenv("ACP_TEST_VERSION", "1")
                                ),
                                "agentCapabilities": {
                                    "loadSession": True,
                                    "sessionCapabilities": {"list": {}, "resume": {}},
                                    "promptCapabilities": {"image": True},
                                    "mcpCapabilities": {"http": True},
                                },
                                "agentInfo": {"name": "scripted", "version": "1"},
                                "_meta": {"peer": "retained"},
                            },
                        }
                    )
                case "session/new":
                    session_count += 1
                    send(
                        {
                            "id": message["id"],
                            "result": {
                                "sessionId": f"native-{session_count}",
                                "modes": {
                                    "currentModeId": "default",
                                    "availableModes": [
                                        {"id": "default", "name": "Default"}
                                    ],
                                },
                                "_meta": {"providerSession": True},
                            },
                        }
                    )
                case "session/prompt" if message["params"].get("_meta", {}).get("fail"):
                    send(
                        {
                            "id": message["id"],
                            "error": {
                                "code": -32602,
                                "message": "scripted refusal",
                                "data": {"retry": False},
                            },
                        }
                    )
                case "session/prompt":
                    pending = message["id"]
                    session_id = message["params"]["sessionId"]
                    callback_method = (
                        message["params"]
                        .get("_meta", {})
                        .get("callbackMethod", "session/request_permission")
                    )
                    send(
                        {
                            "method": "session/update",
                            "params": {
                                "sessionId": session_id,
                                "update": {
                                    "sessionUpdate": "agent_message_chunk",
                                    "content": {"type": "text", "text": "x" * 100_000},
                                },
                            },
                        }
                    )
                    send(
                        {
                            "id": "permission",
                            "method": callback_method,
                            "params": {
                                "session/request_permission": {
                                    "sessionId": session_id,
                                    "toolCall": {"toolCallId": "edit", "title": "Edit"},
                                    "options": [
                                        {
                                            "optionId": "yes",
                                            "name": "Allow",
                                            "kind": "allow_once",
                                        }
                                    ],
                                },
                                "fs/read_text_file": {
                                    "sessionId": session_id,
                                    "path": "/project/file.txt",
                                },
                                "terminal/create": {
                                    "sessionId": session_id,
                                    "command": "echo",
                                    "args": ["hello"],
                                },
                            }[callback_method],
                        }
                    )
                case "session/cancel":
                    send({"id": pending, "result": {"stopReason": "cancelled"}})
                    pending = None
                case "_fail":
                    send(
                        {
                            "id": message["id"],
                            "error": {"code": -32602, "message": "scripted refusal"},
                        }
                    )
                case None if message["id"] == "permission" and pending is not None:
                    send({"id": pending, "result": {"stopReason": "end_turn"}})
                    pending = None
                case _:
                    send(
                        {
                            "id": message["id"],
                            "error": {"code": -32601, "message": "Unknown method"},
                        }
                    )


if __name__ == "__main__":
    main()
