from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import TracebackType
from typing import ClassVar
from uuid import uuid4

from pydantic_ai.exceptions import AgentRunError

from octomate.config import ZcodeConfig
from octomate.tentacles.zcode.client import ZcodeClient
from octomate.tentacles.zcode.wire import SessionEvent
from octomate.types.json import JsonObject, JsonValue


def desktop_config(path: Path) -> ZcodeConfig:
    file = path / "desktop.json"
    file.write_text(
        json.dumps(
            {
                "provider": {
                    "builtin:bigmodel": {
                        "kind": "anthropic",
                        "options": {
                            "apiKey": "secret-test-key",
                            "baseURL": "https://example.test/anthropic",
                        },
                        "models": {
                            "GLM-5.3": {
                                "reasoning": {
                                    "enabled": True,
                                    "variants": ["low", "high", "max"],
                                    "defaultVariant": "max",
                                },
                                "limit": {"context": 1000000, "output": 128000},
                                "modalities": {"input": ["text", "image"]},
                            }
                        },
                    }
                }
            }
        )
    )
    return ZcodeConfig(
        models={"GLM-5.3"}, desktop_config=file, state_dir=path / "state"
    )


def history_message(message_id: str, text: str, *, user: bool = False) -> JsonObject:
    model: JsonObject = {"providerID": "builtin:bigmodel", "modelID": "GLM-5.3"}
    return {
        "info": {
            "id": message_id,
            "role": "user" if user else "assistant",
            **({"model": model} if user else model),
        },
        "parts": [{"type": "text", "text": text}],
    }


def event(
    kind: str,
    payload: JsonObject,
    *,
    turn_id: str = "turn-1",
    event_id: str | None = None,
) -> SessionEvent:
    return SessionEvent(
        type=kind,
        payload=payload,
        turn_id=turn_id,
        session_id="session-1",
        event_id=event_id or str(uuid4()),
        seq=1,
    )


class FakeZcodeClient(ZcodeClient):
    instances: ClassVar[list[FakeZcodeClient]] = []
    history: ClassVar[dict[str, list[JsonObject]]] = {}
    prompted: ClassVar[asyncio.Event | None] = None
    calls: list[tuple[str, JsonObject]]
    session_id: str
    closed: bool

    async def __aenter__(self) -> FakeZcodeClient:
        self.calls = []
        self.closed = False
        self.instances.append(self)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True

    async def call(self, method: str, params: JsonObject) -> JsonValue:
        self.calls.append((method, params))
        if method == "session/create":
            self.session_id = str(uuid4())
            self.history[self.session_id] = []
            return {"session": {"sessionId": self.session_id}}
        if method == "session/resume":
            session_id = params["sessionId"]
            assert isinstance(session_id, str)
            if session_id not in self.history:
                raise AgentRunError("Session not found")
            self.session_id = session_id
            return {"session": {"sessionId": self.session_id}}
        if method == "session/send":
            if self.prompted is not None:
                self.prompted.set()
            prompt = params["content"]
            input_id = params["inputId"]
            assert isinstance(prompt, str)
            assert isinstance(input_id, str)
            started = event(
                "turn.started", {"inputId": input_id, "messageId": input_id}
            )
            started.session_id = self.session_id
            self.events.put_nowait(started)
            delta = event(
                "model.streaming",
                {
                    "kind": "text_delta",
                    "delta": "partial",
                    "assistantMessageId": "answer",
                },
            )
            delta.session_id = self.session_id
            self.events.put_nowait(delta)
            self.history[self.session_id].extend(
                [
                    history_message(input_id, prompt, user=True),
                    history_message("answer-" + input_id, "canonical " + prompt),
                ]
            )
            if prompt == "fail":
                self.fail(AgentRunError("broken pipe"))
            elif prompt != "hang":
                completed = event(
                    "turn.completed",
                    {
                        "response": "canonical " + prompt,
                        "resultType": "success",
                        "usage": {
                            "inputTokens": 10,
                            "outputTokens": 4,
                            "modelRequestCount": 1,
                        },
                    },
                )
                completed.session_id = self.session_id
                self.events.put_nowait(completed)
            return {"accepted": True}
        if method == "session/messages":
            messages: list[JsonValue] = list(self.history[self.session_id])
            return {"messages": messages}
        return {}
