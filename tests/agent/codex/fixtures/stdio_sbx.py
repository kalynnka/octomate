"""A scripted app-server behind the real SbxHabitat stdio attachment."""

import json
import os
import signal
import sys
from pathlib import Path

from pydantic import JsonValue

args = sys.argv[1:]
assert args[:2] == ["--debug=false", "exec"]
index = 2
while args[index].startswith("--"):
    key, _, value = args[index].partition("=")
    if key == "--env":
        name, _, content = value.partition("=")
        os.environ[name] = content
    elif key == "--workdir":
        os.chdir(value)
    index += 1
info = {
    "pid": os.getpid(),
    "argv": args[index + 1 :],
    "environment": {
        key: value
        for key, value in os.environ.items()
        if key in {"PWD", "CODEX_HOME", "CORE_ONLY"}
    },
}
Path(os.environ["CODEX_TEST_LOG"]).write_text(json.dumps(info))
print("guest diagnostic", file=sys.stderr, flush=True)
mode = os.environ.get("CODEX_TEST_MODE", "normal")


def send(payload: dict[str, JsonValue]) -> None:
    print(json.dumps(payload), flush=True)


for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        if mode == "hang":
            signal.pause()
        if mode == "exit":
            sys.exit(7)
        if mode in {"invalid", "partial", "oversize"}:
            frame = {
                "invalid": '"private-payload"\n',
                "partial": '{"private-payload":',
                "oversize": "x" * (1024 * 1024 + 1),
            }[mode]
            sys.stdout.write(frame)
            sys.stdout.flush()
            sys.exit(0)
        send({"id": request["id"], "result": {"userAgent": "codex/0.162.1"}})
        continue
    if method == "probe":
        if request.get("params", {}).get("ask"):
            send({"id": "approval", "method": "approve", "params": {}})
            send({"method": "skills/changed", "params": {}})
        send({"id": request["id"], "result": {**info, "trace": request.get("trace")}})
        continue
    if method == "hang":
        send({"method": "skills/changed", "params": {}})
        continue
    if request.get("id") == "approval":
        send({"method": "skills/changed", "params": {}})

if mode == "close-failure":
    sys.exit(7)
