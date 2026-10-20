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
    stored = Path(os.environ["ACP_TEST_LOG"]).with_suffix(".sessions.json")
    sessions: dict[str, str] = json.loads(stored.read_text()) if stored.exists() else {}
    session_count = len(sessions)
    boolean_options = False
    configurations: dict[str, list[dict[str, JsonValue]]] = {}
    with Path(os.environ["ACP_TEST_LOG"]).open("w") as log:
        for line in sys.stdin:
            message = json.loads(line)
            log.write(json.dumps({"pid": os.getpid(), "message": message}) + "\n")
            log.flush()
            match message.get("method"):
                case "initialize":
                    disabled = (
                        message["params"].get("_meta", {}).get("disableSessions", False)
                    )
                    boolean_options = (
                        message["params"]
                        .get("clientCapabilities", {})
                        .get("session", {})
                        .get("configOptions", {})
                        .get("boolean")
                    ) is not None
                    send(
                        {
                            "id": message["id"],
                            "result": {
                                "protocolVersion": int(
                                    os.getenv("ACP_TEST_VERSION", "1")
                                ),
                                "agentCapabilities": {
                                    "loadSession": not disabled,
                                    "sessionCapabilities": {}
                                    if disabled
                                    else {"list": {}, "resume": {}},
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
                    session_id = f"native-{session_count}"
                    sessions[session_id] = message["params"]["cwd"]
                    stored.write_text(json.dumps(sessions))
                    configurations[session_id] = [
                        {
                            "id": "mode",
                            "name": "Mode",
                            "category": "mode",
                            "type": "select",
                            "currentValue": "default",
                            "options": [
                                {"value": "default", "name": "Default"},
                                {"value": "plan", "name": "Plan"},
                            ],
                            "_meta": {"providerOption": True},
                        },
                        *(
                            [
                                {
                                    "id": "verbose",
                                    "name": "Verbose",
                                    "type": "boolean",
                                    "currentValue": False,
                                }
                            ]
                            if boolean_options
                            else []
                        ),
                    ]
                    send(
                        {
                            "id": message["id"],
                            "result": {
                                "sessionId": session_id,
                                "modes": {
                                    "currentModeId": "default",
                                    "availableModes": [
                                        {"id": "default", "name": "Default"},
                                        {"id": "plan", "name": "Plan"},
                                    ],
                                },
                                "configOptions": [*configurations[session_id]],
                                "_meta": {"providerSession": True},
                            },
                        }
                    )
                case (
                    "session/prompt"
                    | "session/set_mode"
                    | "session/set_config_option"
                    | "session/load"
                    | "session/resume"
                ) if message["params"].get("_meta", {}).get("fail"):
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
                case "session/list":
                    send(
                        {
                            "id": message["id"],
                            "result": {
                                "sessions": [
                                    {"sessionId": key, "cwd": cwd, "title": key}
                                    for key, cwd in {
                                        "private-session": "/private",
                                        **sessions,
                                    }.items()
                                    if message["params"].get("cwd") in (None, cwd)
                                ],
                                "_meta": {"providerList": True},
                            },
                        }
                    )
                case "session/load" | "session/resume":
                    session_id = message["params"]["sessionId"]
                    if session_id not in sessions:
                        send(
                            {
                                "id": message["id"],
                                "error": {"code": -32602, "message": "Unknown session"},
                            }
                        )
                        continue
                    configurations[session_id] = []
                    if message["method"] == "session/load":
                        send(
                            {
                                "method": "session/update",
                                "params": {
                                    "sessionId": session_id,
                                    "update": {
                                        "sessionUpdate": "agent_message_chunk",
                                        "content": {
                                            "type": "text",
                                            "text": "replayed history",
                                        },
                                    },
                                },
                            }
                        )
                    send(
                        {
                            "id": message["id"],
                            "result": {
                                "configOptions": [],
                                "_meta": {"providerOpen": True},
                            },
                        }
                    )
                case "session/set_mode" | "session/set_config_option":
                    params = message["params"]
                    session_id = params["sessionId"]
                    mode = message["method"] == "session/set_mode"
                    config_id = "mode" if mode else params["configId"]
                    value = params["modeId"] if mode else params["value"]
                    configurations[session_id] = [
                        {**option, "currentValue": value}
                        if option["id"] == config_id
                        else option
                        for option in configurations[session_id]
                    ]
                    send(
                        {
                            "method": "session/update",
                            "params": {
                                "sessionId": session_id,
                                "update": (
                                    {
                                        "sessionUpdate": "current_mode_update",
                                        "currentModeId": value,
                                    }
                                    if mode
                                    else {
                                        "sessionUpdate": "config_option_update",
                                        "configOptions": [*configurations[session_id]],
                                    }
                                ),
                                "_meta": {"providerUpdate": True},
                            },
                        }
                    )
                    send(
                        {
                            "id": message["id"],
                            "result": {
                                **(
                                    {}
                                    if mode
                                    else {
                                        "configOptions": [*configurations[session_id]]
                                    }
                                ),
                                "_meta": {"providerSetting": True},
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
                                "fs/write_text_file": {
                                    "sessionId": session_id,
                                    "path": "/project/file.txt",
                                    "content": "client file",
                                },
                                "terminal/create": {
                                    "sessionId": session_id,
                                    "command": "echo",
                                    "args": ["hello"],
                                },
                                "terminal/output": {
                                    "sessionId": session_id,
                                    "terminalId": "client-terminal",
                                },
                                "terminal/wait_for_exit": {
                                    "sessionId": session_id,
                                    "terminalId": "client-terminal",
                                },
                                "terminal/kill": {
                                    "sessionId": session_id,
                                    "terminalId": "client-terminal",
                                },
                                "terminal/release": {
                                    "sessionId": session_id,
                                    "terminalId": "client-terminal",
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
