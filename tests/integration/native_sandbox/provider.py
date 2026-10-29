"""Script only model responses; native tools and sandbox enforcement stay real."""

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal

from pydantic import BaseModel, Field

from octomate.types.json import JsonObject, JsonValue
from tests.integration.native_sandbox.matrix import ToolOutcome


class ProviderItem(BaseModel):
    type: str
    content: JsonValue = None
    output: JsonValue = None
    is_error: bool = False
    call_id: str | None = None
    tool_use_id: str | None = None


class ProviderMessage(BaseModel):
    content: str | list[ProviderItem]


class ProviderRequest(BaseModel):
    input: list[ProviderItem] = Field(default_factory=list)
    messages: list[ProviderMessage] = Field(default_factory=list)


@dataclass
class ScriptedProvider:
    api: Literal["responses", "anthropic"]
    tool: str = ""
    arguments: JsonObject = field(default_factory=dict)
    outcome: ToolOutcome | None = None
    call_id: str = "call_audit"

    def events(self, request: ProviderRequest) -> list[JsonObject]:
        items = request.input + [
            item
            for message in request.messages
            if isinstance(message.content, list)
            for item in message.content
        ]
        results = [
            item
            for item in items
            if item.type in {"function_call_output", "tool_result"}
            and (item.call_id if self.api == "responses" else item.tool_use_id)
            == self.call_id
        ]
        if results:
            result = results[-1]
            content = result.output if self.api == "responses" else result.content
            text = content if isinstance(content, str) else json.dumps(content)
            self.outcome = ToolOutcome(text, result.is_error)
        if self.api == "anthropic":
            return self.anthropic_events()
        item: JsonObject = (
            {
                "id": "msg_audit",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "done", "annotations": []}],
            }
            if self.outcome is not None
            else {
                "id": "fc_audit",
                "type": "function_call",
                "name": self.tool,
                "call_id": self.call_id,
                "arguments": json.dumps(self.arguments),
                "status": "completed",
            }
        )
        response: JsonObject = {
            "id": "resp_audit",
            "object": "response",
            "status": "in_progress",
            "output": [],
        }
        return [
            {"type": "response.created", "response": response},
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {
                "type": "response.completed",
                "response": {
                    **response,
                    "status": "completed",
                    "output": [item],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                },
            },
        ]

    def anthropic_events(self) -> list[JsonObject]:
        tool = self.outcome is None
        block: JsonObject = (
            {"type": "tool_use", "id": self.call_id, "name": self.tool, "input": {}}
            if tool
            else {"type": "text", "text": ""}
        )
        delta: JsonObject = (
            {"type": "input_json_delta", "partial_json": json.dumps(self.arguments)}
            if tool
            else {"type": "text_delta", "text": "done"}
        )
        return [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_audit",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": block},
            {"type": "content_block_delta", "index": 0, "delta": delta},
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": "tool_use" if tool else "end_turn",
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]


@contextmanager
def local_provider(state: ScriptedProvider) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: str) -> None:
            pass

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path.endswith("count_tokens"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"input_tokens":1}')
                return
            events = state.events(ProviderRequest.model_validate_json(body))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in events:
                self.wfile.write(
                    f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                )
            self.wfile.flush()

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=5)
