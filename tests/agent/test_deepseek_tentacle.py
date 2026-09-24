"""`DeepseekTentacle` drives one fake `/api` gateway: the process and client are
monkeypatched fakes, the mux stream is an in-memory queue the fake feeds when
`session/prompt` (or `respond`) is called — mirroring the real gateway, where
prompting is what makes a turn's events flow."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, cast

import anyio
import pytest
from logfire.testing import CaptureLogfire
from logfire.testing import capfire as capfire
from octomate_protocol.deepseek import (
    ErrResult,
    OkResult,
    RpcError,
    RpcResult,
)
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, use_span
from pydantic import HttpUrl, SecretStr
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import ModelMessage, PartStartEvent, TextPart
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import ChannelConfig
from octomate.config.agents import DeepseekConfig
from octomate.database import async_session
from octomate.managers.deferred import DeferredActionManager
from octomate.managers.workspaces.base import ChatWorkspace
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.tentacles.deepseek import DeepseekTentacle
from octomate.tentacles.deepseek import base as deepseek_base
from octomate.tentacles.deepseek.client import DeepseekApiClient
from octomate.tentacles.deepseek.wire import (
    ApprovalRequestedFrame,
    MuxFrame,
    QuestionRequestedFrame,
    RpcReceipt,
    SessionAssistantFrame,
    SessionEventFrame,
    StreamErrorFrame,
)
from octomate.tentacles.feelers.base import Feelers
from octomate.types.json import JsonObject, JsonValue
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import (
    FakeConversation,
    FakeConversationManager,
    FakePresentedBatch,
    a_thread,
)

KEY = ChannelAddress(
    channel_tentacle_id="im", chat_type="dm", chat_id="alice", user_id="alice"
)

_THREAD = uuid.uuid4()

TurnEntry = (
    JsonObject | ApprovalRequestedFrame | QuestionRequestedFrame | StreamErrorFrame
)


def session_event(session_id: str, event: JsonObject) -> SessionEventFrame:
    return SessionEventFrame.model_validate(
        {"type": "session/event", "sessionId": session_id, "event": event}
    )


def turn_events(text: str = "done") -> list[TurnEntry]:
    return [
        {"type": "turn/start", "seq": 1, "time": 1.0, "data": {"turn": 1}},
        {
            "type": "assistant/chunk",
            "seq": 2,
            "time": 1.0,
            "data": {"chunk": {"type": "text-delta", "text": text}},
        },
        {
            "type": "assistant/message",
            "seq": 3,
            "time": 1.0,
            "data": {"message": {"content": [{"type": "text", "text": text}]}},
        },
        {
            "type": "turn/end",
            "seq": 4,
            "time": 1.0,
            "data": {"turn": 1, "reason": {"kind": "completed"}},
        },
    ]


class FakeSocket:
    async def close(self) -> None:
        return None


class FakeDeepseekProcess:
    started: ClassVar[list[FakeDeepseekProcess]] = []
    stopped: ClassVar[int] = 0
    launch_token: ClassVar[SecretStr | None] = None
    # Model an occupied port: startup fails while another runtime keeps serving.
    fail_start: ClassVar[bool] = False

    def __init__(
        self,
        *,
        executable: str,
        port: int,
        extra_args: list[str],
        dsh_home: Path,
        ready_timeout: float,
        browser_url: HttpUrl | None,
    ) -> None:
        self.executable = executable
        self.port = port
        self.extra_args = extra_args
        self.dsh_home = dsh_home
        self.ready_timeout = ready_timeout
        self.browser_url = browser_url

    async def start(self) -> HttpUrl:
        FakeDeepseekProcess.started.append(self)
        FakeDeepseekApi.serving.add(f"http://127.0.0.1:{self.port}")
        if FakeDeepseekProcess.fail_start:
            raise RuntimeError("dsh web exited before reporting a URL (code 1)")
        return HttpUrl(f"http://127.0.0.1:{self.port}")

    async def stop(self) -> None:
        FakeDeepseekProcess.stopped += 1


class FakeDeepseekApi:
    """Scripted `/api` carrier. `serving` holds the base URLs where a dsh
    answers `settings/describe` — empty until a fake process starts one.
    `turn_script` frames flow when `session/prompt` is called; `after_respond`
    frames flow when `respond` is — how the real
    gateway behaves around a blocking approval."""

    serving: ClassVar[set[str]] = set()
    results: ClassVar[dict[str, RpcResult]] = {}
    calls: ClassVar[list[tuple[str, JsonValue]]] = []
    responds: ClassVar[list[tuple[str, RpcResult | None]]] = []
    turn_script: ClassVar[list[TurnEntry]] = []
    after_respond: ClassVar[list[TurnEntry]] = []
    outbox: ClassVar[asyncio.Queue[tuple[str, MuxFrame]] | None] = None
    last_session: ClassVar[str | None] = None
    frame_serial: ClassVar[int] = 0
    tokens: ClassVar[list[SecretStr]] = []

    def __init__(self, base_url: HttpUrl, http_client: object) -> None:
        self.base_url = base_url
        self.http_client = http_client

    async def __aenter__(self) -> FakeDeepseekApi:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def answering(self) -> bool:
        return not isinstance(await self.call("settings/describe", {}), ErrResult)

    async def authenticate(self, token: SecretStr) -> None:
        self.tokens.append(token)

    @classmethod
    def reset(cls, turn_script: list[TurnEntry] | None = None) -> None:
        cls.serving = set()
        cls.results = {
            "settings/describe": OkResult(
                value={"provider": "deepseek-official", "model": "deepseek-v4-pro"}
            ),
            "session/modelCatalog": OkResult(
                value={
                    "default": {
                        "provider": "deepseek-official",
                        "model": "deepseek-v4-pro",
                    },
                    "groups": [
                        {
                            "id": "deepseek-official",
                            "name": "DeepSeek",
                            "models": [
                                {
                                    "id": model,
                                    "name": model,
                                    "reasoning": {
                                        "efforts": [
                                            {"id": "off"},
                                            {"id": "high"},
                                            {"id": "max"},
                                        ]
                                    },
                                }
                                for model in ("deepseek-v4-flash", "deepseek-v4-pro")
                            ],
                        }
                    ],
                    "failures": [],
                }
            ),
            "permissionPresets/catalog": OkResult(
                value={
                    "options": [
                        {"value": "workspace-write", "name": "Workspace"},
                        {"value": "danger-full-access", "name": "Full access"},
                    ]
                }
            ),
            "session/create": OkResult(value={"sessionId": "sess-1"}),
            "session/projections": OkResult(value={"values": {}}),
            "session/selectModel": OkResult(value={"selected": {}}),
            "session/prompt": OkResult(value={"accepted": True}),
            "session/cancel": OkResult(value={"accepted": True}),
            "commands/execute": OkResult(
                value={"commandId": "cmd-1", "result": {"kind": "success"}}
            ),
        }
        cls.calls = []
        cls.responds = []
        cls.turn_script = turn_script or []
        cls.after_respond = []
        cls.outbox = asyncio.Queue()
        cls.last_session = None
        cls.frame_serial = 0
        cls.tokens = []

    @classmethod
    def push(cls, session_id: str, entries: list[TurnEntry]) -> None:
        assert cls.outbox is not None
        for entry in entries:
            cls.frame_serial += 1
            rpc_id = f"rpc-{cls.frame_serial}"
            if isinstance(entry, dict):
                cls.outbox.put_nowait((rpc_id, session_event(session_id, entry)))
            else:
                cls.outbox.put_nowait((rpc_id, entry))

    async def call(self, method: str, payload: JsonValue) -> RpcResult:
        FakeDeepseekApi.calls.append((method, payload))
        if (
            method == "settings/describe"
            and str(self.base_url).rstrip("/") not in FakeDeepseekApi.serving
        ):
            return ErrResult(
                error=RpcError(code="internal", message=f"nothing at {self.base_url}")
            )
        if method == "session/prompt" and isinstance(payload, dict):
            session_id = cast(str, payload["sessionId"])
            FakeDeepseekApi.last_session = session_id
            FakeDeepseekApi.push(session_id, FakeDeepseekApi.turn_script)
        return FakeDeepseekApi.results[method]

    async def remote(self, endpoint: str, args: JsonObject) -> RpcResult:
        return await self.call(
            endpoint, args["request"] if "request" in args else {"args": args}
        )

    async def respond(self, rpc_id: str, result: RpcResult | None) -> RpcReceipt:
        FakeDeepseekApi.responds.append((rpc_id, result))
        if FakeDeepseekApi.after_respond and FakeDeepseekApi.last_session is not None:
            FakeDeepseekApi.push(
                FakeDeepseekApi.last_session, FakeDeepseekApi.after_respond
            )
            FakeDeepseekApi.after_respond = []
        return RpcReceipt(accepted=True)

    async def follow(self, socket: FakeSocket, session_id: str) -> None:
        return None

    async def unfollow(self, socket: FakeSocket, session_id: str) -> None:
        return None

    async def open_mux(self) -> FakeSocket:
        return FakeSocket()

    async def mux_frames(self, socket: object) -> AsyncIterator[tuple[str, MuxFrame]]:
        assert FakeDeepseekApi.outbox is not None
        while True:
            yield await FakeDeepseekApi.outbox.get()


def calls_of(method: str) -> list[JsonValue]:
    return [payload for name, payload in FakeDeepseekApi.calls if name == method]


@dataclass
class FakeFeelers:
    batch: FakePresentedBatch
    requests: list[object] = field(default_factory=list)
    presented: asyncio.Event = field(default_factory=asyncio.Event)

    async def present_actions(
        self, *, requests: object, **_: object
    ) -> FakePresentedBatch:
        self.requests.append(requests)
        self.presented.set()
        return self.batch


def a_channel(feelers: FakeFeelers) -> FakeChannelTentacle:
    """The `im` channel the tentacle presents approvals and questions through,
    its feelers recording what was asked."""
    channel = FakeChannelTentacle(config=ChannelConfig(type="fake", agents=["inkling"]))
    channel.feelers = cast(Feelers, feelers)
    return channel


@dataclass
class RecordingDeferredActions:
    resolved: list[DeferredActionBatchResponse] = field(default_factory=list)
    marked: list[tuple[uuid.UUID, str]] = field(default_factory=list)

    async def resolve_batch(self, awake: DeferredActionBatchResponse) -> None:
        self.resolved.append(awake)

    async def mark_batch(
        self, batch_id: uuid.UUID, status: str, *, completed: bool = False
    ) -> None:
        self.marked.append((batch_id, status))


def patch_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deepseek_base, "DeepseekProcess", FakeDeepseekProcess)
    monkeypatch.setattr(deepseek_base, "DeepseekApiClient", FakeDeepseekApi)
    FakeDeepseekProcess.started = []
    FakeDeepseekProcess.stopped = 0
    FakeDeepseekProcess.fail_start = False
    FakeDeepseekProcess.launch_token = None


def _tentacle(
    conversations: FakeConversationManager,
    *,
    config: DeepseekConfig | None = None,
    octomate: Octomate | None = None,
) -> DeepseekTentacle:
    return DeepseekTentacle(
        "deepseek",
        octomate or Octomate(conversations=conversations),
        config=config or DeepseekConfig(),
    )


def bridge_context(
    conversation: FakeConversation, *, interactive: bool = True
) -> deepseek_base.DeepseekBridgeContext:
    return deepseek_base.DeepseekBridgeContext(
        conversation=cast(deepseek_base.Conversation, conversation),
        conversation_address=KEY,
        run_name="react",
        session_allowed=set(conversation.allowed_tools),
        interactive=interactive,
    )


async def wait_for_pending(
    tentacle: DeepseekTentacle, feelers: FakeFeelers
) -> uuid.UUID:
    await asyncio.wait_for(feelers.presented.wait(), timeout=5)
    return next(iter(tentacle.pending))


def interaction_octomate(
    feelers: FakeFeelers,
    deferred_actions: RecordingDeferredActions,
    conversations: FakeConversationManager | None = None,
) -> Octomate:
    return Octomate(
        conversations=conversations or FakeConversationManager(),
        deferred_actions=cast(DeferredActionManager, deferred_actions),
        tentacles={"im": a_channel(feelers)},
    )


@pytest.mark.parametrize("resumed", [False, True])
async def test_driven_names_are_persisted_and_revised(
    monkeypatch: pytest.MonkeyPatch,
    in_memory_engine: AsyncEngine,
    resumed: bool,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events())
    octomate = Octomate()
    thread_id = await a_thread()
    conversation = await octomate.conversations.ensure(
        thread_id, agent_tentacle_id="deepseek"
    )
    session_id = "prior-session" if resumed else "sess-1"
    if resumed:
        async with async_session() as session:
            stored = await session.get(Conversation, conversation.id)
            assert stored is not None
            stored.external_id = session_id
            await session.commit()
    tentacle = DeepseekTentacle("deepseek", octomate, config=DeepseekConfig())
    names = [None, "", "  ", " First name ", "修复会话名称", "修复会话名称", None, " "]
    expected = None
    async with tentacle:
        for name in names:
            FakeDeepseekApi.results["session/projections"] = OkResult(
                value={"asOfSeq": 5, "values": {"title": name}}
            )
            result = await tentacle.run(
                "work", conversation_address=KEY, thread_id=thread_id
            )
            assert result.output == "done"
            if name and name.strip():
                expected = name.strip()
            stored = await octomate.conversations.get(
                conversation.id, with_history=False
            )
            thread = await octomate.thread_manager.get(thread_id, with_messages=False)
            assert thread is not None
            assert stored.name == expected
            assert thread.title == expected
    assert calls_of("session/projections") == [{"sessionId": session_id}] * len(names)
    assert len(calls_of("session/create")) == (0 if resumed else 1)


async def test_driven_child_name_does_not_rename_parent_thread(
    monkeypatch: pytest.MonkeyPatch, in_memory_engine: AsyncEngine
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events())
    FakeDeepseekApi.results["session/projections"] = OkResult(
        value={"values": {"title": "Child work"}}
    )
    octomate = Octomate()
    thread_id = await a_thread()
    thread = await octomate.thread_manager.get(thread_id, with_messages=False)
    assert thread is not None
    await octomate.thread_manager.rename(thread, "Parent work")
    parent = await octomate.conversations.ensure(
        thread_id, agent_tentacle_id="deepseek"
    )
    await octomate.conversations.set_name(parent, "Parent work")
    child = await octomate.conversations.ensure(
        thread_id,
        agent_tentacle_id="deepseek",
        subagent_id="child",
        parent_conversation_id=parent.id,
    )
    tentacle = DeepseekTentacle("deepseek", octomate, config=DeepseekConfig())
    async with tentacle:
        await tentacle.run(
            "work",
            conversation_address=KEY,
            thread_id=thread_id,
            conversation_id=child.id,
        )
    assert (
        await octomate.conversations.get(child.id, with_history=False)
    ).name == "Child work"
    assert (
        await octomate.conversations.get(parent.id, with_history=False)
    ).name == "Parent work"
    thread = await octomate.thread_manager.get(thread_id, with_messages=False)
    assert thread is not None
    assert thread.title == "Parent work"


@pytest.mark.parametrize(
    "lookup",
    [
        ErrResult(error=RpcError(code="internal", message="lookup failed")),
        OkResult(value={"values": {"title": 42}}),
        OkResult(value=None),
    ],
)
async def test_driven_name_lookup_failure_or_missing_session_keeps_result(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    lookup: RpcResult,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events())
    FakeDeepseekApi.results["session/projections"] = lookup
    conversations = FakeConversationManager()
    tentacle = _tentacle(conversations)
    async with tentacle:
        result = await tentacle.run("work", conversation_address=KEY, thread_id=_THREAD)
    assert result.output == "done"
    assert len(conversations.runs) == 1
    if isinstance(lookup, ErrResult) or lookup.value is not None:
        assert "dsh session name lookup failed for sess-1" in caplog.text


@pytest.mark.parametrize("instrument", [False, True])
async def test_run_stream_events_creates_session_proxies_events_and_persists(
    monkeypatch: pytest.MonkeyPatch,
    instrument: bool,
    capfire: CaptureLogfire,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events("done"))
    conversations = FakeConversationManager()
    tentacle = _tentacle(conversations, config=DeepseekConfig(instrument=instrument))

    events = []
    with use_span(NonRecordingSpan(SpanContext(91, 92, False, TraceFlags(1)))):
        async with tentacle:
            async with tentacle.run_stream_events(
                "fix it",
                conversation_address=KEY,
                thread_id=_THREAD,
                run_name="react",
                model="deepseek-v4-pro",
                effort="xhigh",
            ) as stream:
                async for event in stream:
                    events.append(event)

    assert any(isinstance(event, PartStartEvent) for event in events)
    assert isinstance(events[-1], AgentRunResultEvent)
    assert events[-1].result.output == "done"
    records = [
        span
        for span in capfire.exporter.exported_spans
        if span.attributes and "event_type" in span.attributes
    ]
    assert [span.attributes["event_type"] for span in records if span.attributes] == (
        ["turn/start", "assistant/message", "turn/end"] if instrument else []
    )
    [driving_span] = [
        span
        for span in capfire.exporter.exported_spans
        if span.name.startswith("DeepseekTentacle ")
        and span.attributes
        and span.attributes.get("logfire.span_type") == "span"
    ]
    assert driving_span.context is not None
    assert driving_span.context.trace_id == 91
    assert driving_span.parent is not None
    assert driving_span.parent.span_id == 92
    for record in records:
        assert record.context is not None
        assert record.context.trace_id == 91
        assert record.parent == driving_span.context

    [create_payload] = calls_of("session/create")
    # A thread in no project runs in a workspace forked for the run, not at the
    # configured `cwd` — which defaults to `"."`, Octomate's own directory.
    assert create_payload == {
        "cwd": str(tentacle.octomate.workspaces.open(_THREAD, None).path)
    }
    [select_payload] = calls_of("session/selectModel")
    assert select_payload == {
        "sessionId": "sess-1",
        "provider": "deepseek-official",
        "model": "deepseek-v4-pro",
        "reasoningEffort": "max",
    }
    [permission_payload] = calls_of("commands/execute")
    assert permission_payload == {
        "args": {
            "agentId": "sess-1",
            "line": "/permission workspace-write",
            "submittedAttachments": [],
        }
    }
    [prompt_payload] = calls_of("session/prompt")
    assert isinstance(prompt_payload, dict)
    assert uuid.UUID(str(prompt_payload.pop("requestId")))
    assert prompt_payload == {
        "sessionId": "sess-1",
        "mode": "queue",
        "content": [{"type": "text", "text": "fix it"}],
    }

    [recorded] = conversations.runs
    fake, _label, messages = recorded
    assert fake.external_id == "sess-1"
    assert fake.runs[-1].native_id == "deepseek-native"
    assert fake.runs[-1].native_session_id == "sess-1"
    assert fake.runs[-1].native_turn_id == "sess-1:1"
    assert messages


async def test_run_reuses_the_stored_session_and_skips_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events("again"))
    conversations = FakeConversationManager()
    conversations.store[(_THREAD, "deepseek", "")] = FakeConversation(
        thread_id=_THREAD, external_id="sess-old"
    )
    tentacle = _tentacle(conversations)

    async with tentacle:
        result = await tentacle.run("more", conversation_address=KEY, thread_id=_THREAD)

    assert result.output == "again"
    assert not calls_of("session/create")
    [prompt_payload] = calls_of("session/prompt")
    assert isinstance(prompt_payload, dict)
    assert prompt_payload["sessionId"] == "sess-old"


async def test_agent_preset_and_the_chat_cwd_reach_session_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events())
    tentacle = _tentacle(
        FakeConversationManager(),
        config=DeepseekConfig(agent_preset="octopus"),
    )

    async with tentacle:
        await tentacle.run("go", conversation_address=KEY, thread_id=_THREAD)

    [create_payload] = calls_of("session/create")
    assert create_payload == {
        "cwd": str(tentacle.octomate.workspaces.open(_THREAD, None).path),
        "agentPreset": "octopus",
    }
    # And the run is what ends it: a thread in no project keeps nothing.
    assert not tentacle.octomate.workspaces.open(_THREAD, None).path.exists()


async def test_without_a_model_the_session_selection_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events())
    tentacle = _tentacle(FakeConversationManager())

    async with tentacle:
        await tentacle.run("go", conversation_address=KEY, thread_id=_THREAD)

    assert not calls_of("session/selectModel")


@pytest.mark.parametrize("mode", ["danger-full-access", "audit-only"])
async def test_the_conversations_posture_overrides_the_configured_one(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events())
    FakeDeepseekApi.results["permissionPresets/catalog"] = OkResult(
        value={
            "options": [
                {"value": "workspace-write", "name": "Workspace"},
                {"value": mode, "name": "Custom mode"},
            ]
        }
    )
    conversations = FakeConversationManager()
    conversations.store[(_THREAD, "deepseek", "")] = FakeConversation(
        thread_id=_THREAD, permission_mode=mode
    )
    tentacle = _tentacle(conversations)

    async with tentacle:
        await tentacle.run("go", conversation_address=KEY, thread_id=_THREAD)

    [permission_payload] = calls_of("commands/execute")
    assert isinstance(permission_payload, dict)
    assert permission_payload["args"] == {
        "agentId": "sess-1",
        "line": f"/permission {mode}",
        "submittedAttachments": [],
    }


async def test_an_unavailable_posture_fails_before_prompting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events())
    conversations = FakeConversationManager()
    conversations.store[(_THREAD, "deepseek", "")] = FakeConversation(
        thread_id=_THREAD, permission_mode="bypassPermissions"
    )
    tentacle = _tentacle(conversations)
    async with tentacle:
        with pytest.raises(ValueError, match="not one of deepseek's modes"):
            await tentacle.run("go", conversation_address=KEY, thread_id=_THREAD)
    assert not calls_of("commands/execute")
    assert not calls_of("session/prompt")


async def test_instructions_frame_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events())
    tentacle = _tentacle(FakeConversationManager())

    async with tentacle:
        await tentacle.run(
            "work the brief",
            conversation_address=KEY,
            thread_id=_THREAD,
            instructions="You are an accomplice.",
        )

    [prompt_payload] = calls_of("session/prompt")
    assert isinstance(prompt_payload, dict)
    # Marked, because dsh has no instructions channel and everything else in the
    # prompt is what somebody said — the brief stays the body.
    assert prompt_payload["content"] == [
        {
            "type": "text",
            "text": (
                "<instructions>\nYou are an accomplice.\n</instructions>"
                "\n\nwork the brief"
            ),
        }
    ]


async def test_output_type_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events())
    tentacle = _tentacle(FakeConversationManager())

    async with tentacle:
        with pytest.raises(ValueError, match="structured output"):
            await tentacle.run(
                "shape this",
                conversation_address=KEY,
                thread_id=_THREAD,
                output_type=dict[str, str],
            )


async def test_slash_text_is_a_normal_prompt_on_the_remote_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events("normal model response"))
    tentacle = _tentacle(FakeConversationManager())
    async with tentacle:
        result = await tentacle.run(
            "/permission workspace-write", conversation_address=KEY, thread_id=_THREAD
        )
    assert result.output == "normal model response"
    assert not calls_of("session/cancel")


async def test_a_refused_rpc_becomes_the_runs_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events())
    FakeDeepseekApi.results["session/create"] = ErrResult(
        error=RpcError(code="internal", message="no adapters")
    )
    tentacle = _tentacle(FakeConversationManager())

    async with tentacle:
        with pytest.raises(AgentRunError, match="no adapters"):
            await tentacle.run("go", conversation_address=KEY, thread_id=_THREAD)


async def test_an_unknown_preset_is_refused_not_run_under(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events())
    FakeDeepseekApi.results["commands/execute"] = OkResult(
        value={
            "commandId": "cmd-1",
            "result": {"kind": "error", "text": "unknown preset"},
        }
    )
    tentacle = _tentacle(FakeConversationManager())

    async with tentacle:
        with pytest.raises(AgentRunError, match="unknown preset"):
            await tentacle.run("go", conversation_address=KEY, thread_id=_THREAD)


async def test_a_mid_turn_stream_error_persists_cancels_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(
        [
            {"type": "turn/start", "seq": 1, "time": 1.0, "data": {"turn": 1}},
            {
                "type": "assistant/chunk",
                "seq": 2,
                "time": 1.0,
                "data": {"chunk": {"type": "text-delta", "text": "part"}},
            },
            StreamErrorFrame(
                type="stream/error",
                error=RpcError(code="internal", message="socket died"),
            ),
        ]
    )
    conversations = FakeConversationManager()
    tentacle = _tentacle(conversations)

    async with tentacle:
        with pytest.raises(AgentRunError, match="socket died"):
            await tentacle.run("go", conversation_address=KEY, thread_id=_THREAD)

    # The partial turn persisted before the raise, and dsh's live turn was told
    # to stop burning.
    [recorded] = conversations.runs
    _fake, _label, messages = recorded
    assert messages
    [cancel_payload] = calls_of("session/cancel")
    assert cancel_payload == {"sessionId": "sess-1"}


@pytest.mark.parametrize("cancel_fails", [False, True])
async def test_driving_covers_runtime_cleanup_before_persistence_and_workspace_exit(
    monkeypatch: pytest.MonkeyPatch, cancel_fails: bool
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(
        [
            {"type": "turn/start", "seq": 1, "time": 1.0, "data": {"turn": 1}},
            StreamErrorFrame(
                type="stream/error",
                error=RpcError(code="internal", message="socket died"),
            ),
        ]
    )
    conversations = FakeConversationManager()
    tentacle = _tentacle(conversations)
    tentacle.octomate.connect(tentacle)
    original_call = FakeDeepseekApi.call
    original_claim = tentacle.octomate.workspaces.claim
    original_discard = tentacle.octomate.workspaces.discard
    original_record = conversations.record_agent_run
    observed: list[str] = []

    async def claim(workspace: ChatWorkspace) -> None:
        assert tentacle.driven_sessions == {}
        observed.append("workspace.enter")
        await original_claim(workspace)

    async def discard(workspace: ChatWorkspace) -> None:
        assert tentacle.driven_sessions == {}
        observed.append("workspace.exit")
        await original_discard(workspace)

    async def record(
        conversation: Conversation,
        *,
        run_id: str,
        messages: Sequence[ModelMessage],
        name: str | None,
        cwd: Path,
        external_id: str,
        native_id: str,
        native_turn_id: str | None,
    ) -> None:
        assert tentacle.driven_sessions == {}
        observed.append("record")
        await original_record(
            conversation,
            run_id,
            messages,
            name=name,
            cwd=cwd,
            external_id=external_id,
            native_id=native_id,
            native_turn_id=native_turn_id,
        )

    async def call(
        client: FakeDeepseekApi, method: str, payload: JsonValue
    ) -> RpcResult:
        if method == "session/create":
            assert tentacle.driven_sessions == {}
            observed.append(method)
        if method in {"session/prompt", "session/cancel"}:
            assert isinstance(payload, dict)
            session_id = payload["sessionId"]
            assert isinstance(session_id, str)
            assert tentacle.driven_sessions == {session_id: 1}
            observed.append(method)
            if method == "session/cancel" and cancel_fails:
                raise RuntimeError("cancel failed")
        return await original_call(client, method, payload)

    monkeypatch.setattr(FakeDeepseekApi, "call", call)
    monkeypatch.setattr(tentacle.octomate.workspaces, "claim", claim)
    monkeypatch.setattr(tentacle.octomate.workspaces, "discard", discard)
    monkeypatch.setattr(conversations, "record_agent_run", record)
    async with tentacle:
        with pytest.raises(AgentRunError, match="socket died"):
            await tentacle.run("go", conversation_address=KEY, thread_id=_THREAD)
        assert tentacle.subscribers == {}
        assert tentacle.bridge_contexts == {}
        assert tentacle.driven_sessions == {}

    assert observed == [
        "workspace.enter",
        "session/create",
        "session/prompt",
        "session/cancel",
        "record",
        "workspace.exit",
    ]


async def test_an_error_turn_raises_dshs_own_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(
        [
            {"type": "turn/start", "seq": 1, "time": 1.0, "data": {"turn": 1}},
            {
                "type": "turn/end",
                "seq": 2,
                "time": 1.0,
                "data": {
                    "turn": 1,
                    "reason": {"kind": "error", "error": {"message": "rate limited"}},
                },
            },
        ]
    )
    conversations = FakeConversationManager()
    tentacle = _tentacle(conversations)

    async with tentacle:
        with pytest.raises(AgentRunError, match="rate limited"):
            await tentacle.run("go", conversation_address=KEY, thread_id=_THREAD)

    assert conversations.runs
    assert not calls_of("session/cancel")


async def test_an_approval_mid_run_bridges_to_a_card_and_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(
        [
            {"type": "turn/start", "seq": 1, "time": 1.0, "data": {"turn": 1}},
            ApprovalRequestedFrame(
                type="approval/requested",
                session_id="sess-1",
                approval_id="ap-1",
                tool_name="bash",
                reason="rm -rf outside the workspace",
            ),
        ]
    )
    FakeDeepseekApi.after_respond = turn_events("released")[1:]
    approval = DeferredApproval(
        tool_name="bash",
        tool_call_id="ap-1",
        args=ApprovalRequest(tool_name="bash"),
    )
    feelers = FakeFeelers(batch=FakePresentedBatch(approvals=[approval]))
    deferred_actions = RecordingDeferredActions()
    conversations = FakeConversationManager()
    octomate = interaction_octomate(feelers, deferred_actions, conversations)
    tentacle = _tentacle(conversations, octomate=octomate)
    octomate.connect(tentacle)

    async with tentacle:
        run = asyncio.ensure_future(
            tentacle.run("dangerous", conversation_address=KEY, thread_id=_THREAD)
        )
        batch_id = await wait_for_pending(tentacle, feelers)
        await octomate.kick(
            DeferredActionBatchResponse(
                batch_id=batch_id, approvals={approval.id: True}
            )
        )
        result = await run

    assert result.output == "released"
    assert len(feelers.requests) == 1
    [(rpc_id, response)] = FakeDeepseekApi.responds
    assert rpc_id == "rpc-2"
    assert isinstance(response, OkResult)
    assert response.value == "allowed-once"


async def test_a_declined_approval_answers_rejected() -> None:
    FakeDeepseekApi.reset()
    approval = DeferredApproval(
        tool_name="bash", tool_call_id="ap-1", args=ApprovalRequest(tool_name="bash")
    )
    feelers = FakeFeelers(batch=FakePresentedBatch(approvals=[approval]))
    deferred_actions = RecordingDeferredActions()
    octomate = interaction_octomate(feelers, deferred_actions)
    tentacle = _tentacle(FakeConversationManager(), octomate=octomate)
    octomate.connect(tentacle)
    tentacle.client = cast(
        DeepseekApiClient, FakeDeepseekApi(HttpUrl("http://t"), None)
    )
    conversation = FakeConversation(thread_id=_THREAD)
    tentacle.bridge_contexts["sess-1"] = bridge_context(conversation)
    frame = ApprovalRequestedFrame(
        type="approval/requested",
        session_id="sess-1",
        approval_id="ap-1",
        tool_name="bash",
    )

    task = asyncio.create_task(tentacle.answer_interaction("rpc-9", frame))
    batch_id = await wait_for_pending(tentacle, feelers)
    await octomate.kick(
        DeferredActionBatchResponse(batch_id=batch_id, approvals={approval.id: False})
    )
    await task

    [(rpc_id, response)] = FakeDeepseekApi.responds
    assert rpc_id == "rpc-9"
    assert isinstance(response, OkResult)
    assert response.value == "rejected"


async def test_an_expired_approval_answers_cancelled() -> None:
    FakeDeepseekApi.reset()
    approval = DeferredApproval(
        tool_name="bash", tool_call_id="ap-1", args=ApprovalRequest(tool_name="bash")
    )
    feelers = FakeFeelers(batch=FakePresentedBatch(approvals=[approval]))
    deferred_actions = RecordingDeferredActions()
    octomate = interaction_octomate(feelers, deferred_actions)
    tentacle = _tentacle(
        FakeConversationManager(),
        config=DeepseekConfig(approval_timeout=0.01),
        octomate=octomate,
    )
    octomate.connect(tentacle)
    tentacle.client = cast(
        DeepseekApiClient, FakeDeepseekApi(HttpUrl("http://t"), None)
    )
    tentacle.bridge_contexts["sess-1"] = bridge_context(
        FakeConversation(thread_id=_THREAD)
    )
    frame = ApprovalRequestedFrame(
        type="approval/requested",
        session_id="sess-1",
        approval_id="ap-1",
        tool_name="bash",
    )

    await tentacle.answer_interaction("rpc-9", frame)

    [(rpc_id, response)] = FakeDeepseekApi.responds
    assert rpc_id == "rpc-9"
    assert isinstance(response, ErrResult)
    assert response.error.code == "cancelled"
    assert deferred_actions.marked


async def test_allow_session_short_circuits_the_next_approval() -> None:
    FakeDeepseekApi.reset()
    approval = DeferredApproval(
        tool_name="bash", tool_call_id="ap-1", args=ApprovalRequest(tool_name="bash")
    )
    feelers = FakeFeelers(batch=FakePresentedBatch(approvals=[approval]))
    deferred_actions = RecordingDeferredActions()
    conversations = FakeConversationManager()
    octomate = interaction_octomate(feelers, deferred_actions, conversations)
    tentacle = _tentacle(conversations, octomate=octomate)
    octomate.connect(tentacle)
    tentacle.client = cast(
        DeepseekApiClient, FakeDeepseekApi(HttpUrl("http://t"), None)
    )
    conversation = FakeConversation(thread_id=_THREAD)
    tentacle.bridge_contexts["sess-1"] = bridge_context(conversation)
    frame = ApprovalRequestedFrame(
        type="approval/requested",
        session_id="sess-1",
        approval_id="ap-1",
        tool_name="bash",
    )

    task = asyncio.create_task(tentacle.answer_interaction("rpc-1", frame))
    batch_id = await wait_for_pending(tentacle, feelers)
    await octomate.kick(
        DeferredActionBatchResponse(
            batch_id=batch_id, approvals={approval.id: True}, allow_session=True
        )
    )
    await task
    second = ApprovalRequestedFrame(
        type="approval/requested",
        session_id="sess-1",
        approval_id="ap-2",
        tool_name="bash",
    )
    await tentacle.answer_interaction("rpc-2", second)

    assert conversation.allowed_tools == ["bash"]
    assert len(feelers.requests) == 1
    outcomes = [
        response.value
        for _rpc, response in FakeDeepseekApi.responds
        if isinstance(response, OkResult)
    ]
    assert outcomes == ["allowed-once", "allowed-once"]


async def test_a_non_interactive_run_declines_without_a_card() -> None:
    FakeDeepseekApi.reset()
    feelers = FakeFeelers(batch=FakePresentedBatch())
    octomate = interaction_octomate(feelers, RecordingDeferredActions())
    tentacle = _tentacle(FakeConversationManager(), octomate=octomate)
    octomate.connect(tentacle)
    tentacle.client = cast(
        DeepseekApiClient, FakeDeepseekApi(HttpUrl("http://t"), None)
    )
    tentacle.bridge_contexts["sess-1"] = bridge_context(
        FakeConversation(thread_id=_THREAD), interactive=False
    )

    await tentacle.answer_interaction(
        "rpc-9",
        ApprovalRequestedFrame(
            type="approval/requested",
            session_id="sess-1",
            approval_id="ap-1",
            tool_name="bash",
        ),
    )

    assert not feelers.requests
    [(_rpc, response)] = FakeDeepseekApi.responds
    assert isinstance(response, OkResult)
    assert response.value == "rejected"


async def test_a_request_nobody_drives_is_delegated_to_other_clients() -> None:
    FakeDeepseekApi.reset()
    tentacle = _tentacle(FakeConversationManager())
    tentacle.client = cast(
        DeepseekApiClient, FakeDeepseekApi(HttpUrl("http://t"), None)
    )

    await tentacle.answer_interaction(
        "rpc-9",
        ApprovalRequestedFrame(
            type="approval/requested",
            session_id="sess-ghost",
            approval_id="ap-1",
            tool_name="bash",
        ),
    )

    [(_rpc, response)] = FakeDeepseekApi.responds
    assert response is None


async def test_questions_map_labels_to_selected_and_text_to_custom() -> None:
    FakeDeepseekApi.reset()
    first = DeferredQuestion(
        tool_name="deepseek_user_input",
        tool_call_id="ask-1",
        position=0,
        args={"question": "Which branch?"},
    )
    second = DeferredQuestion(
        tool_name="deepseek_user_input",
        tool_call_id="ask-1",
        position=1,
        args={"question": "Anything else?"},
    )
    feelers = FakeFeelers(batch=FakePresentedBatch(questions=[first, second]))
    deferred_actions = RecordingDeferredActions()
    octomate = interaction_octomate(feelers, deferred_actions)
    tentacle = _tentacle(FakeConversationManager(), octomate=octomate)
    octomate.connect(tentacle)
    tentacle.client = cast(
        DeepseekApiClient, FakeDeepseekApi(HttpUrl("http://t"), None)
    )
    tentacle.bridge_contexts["sess-1"] = bridge_context(
        FakeConversation(thread_id=_THREAD)
    )
    frame = QuestionRequestedFrame.model_validate(
        {
            "type": "question/requested",
            "sessionId": "sess-1",
            "questions": [
                {
                    "id": "q1",
                    "question": "Which branch?",
                    "options": [{"label": "main"}, {"label": "dev"}],
                },
                {"id": "q2", "question": "Anything else?"},
            ],
        }
    )

    task = asyncio.create_task(tentacle.answer_interaction("rpc-9", frame))
    batch_id = await wait_for_pending(tentacle, feelers)
    await octomate.kick(
        DeferredActionBatchResponse(
            batch_id=batch_id,
            answers={first.id: "main", second.id: "ship it"},
        )
    )
    await task

    [(_rpc, response)] = FakeDeepseekApi.responds
    assert isinstance(response, OkResult)
    assert response.value == {
        "answers": [
            {"id": "q1", "selected": ["main"]},
            {"id": "q2", "selected": [], "custom": "ship it"},
        ],
    }


async def test_starts_its_own_runtime_beside_native_dsh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events("shared"))
    FakeDeepseekApi.serving.add("http://127.0.0.1:3080")
    tentacle = _tentacle(FakeConversationManager())

    async with tentacle:
        assert tentacle.process is not None
        assert tentacle.client is not None
        assert tentacle.client.base_url == HttpUrl("http://127.0.0.1:3081")
        result = await tentacle.run("go", conversation_address=KEY, thread_id=_THREAD)

    assert result.output == "shared"
    assert len(FakeDeepseekProcess.started) == 1
    assert FakeDeepseekProcess.stopped == 1


async def test_nothing_serving_starts_a_dsh_on_the_configured_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset()
    tentacle = _tentacle(
        FakeConversationManager(),
        config=DeepseekConfig(
            port=4090, browser_url=HttpUrl("https://dsh.example:8443")
        ),
    )

    async with tentacle:
        pass

    [process] = FakeDeepseekProcess.started
    assert process.port == 4090
    assert process.browser_url == HttpUrl("https://dsh.example:8443")
    assert FakeDeepseekProcess.stopped == 1


async def test_authenticates_with_the_spawned_launch_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset()
    token = SecretStr("private-launch-token")
    FakeDeepseekProcess.launch_token = token
    tentacle = _tentacle(FakeConversationManager())

    async with tentacle:
        assert FakeDeepseekApi.tokens == [token]

    assert FakeDeepseekProcess.stopped == 1


async def test_authentication_failure_stops_the_started_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset()
    FakeDeepseekProcess.launch_token = SecretStr("rejected-token")

    async def reject(self: FakeDeepseekApi, token: SecretStr) -> None:
        raise RuntimeError("launch token rejected")

    monkeypatch.setattr(FakeDeepseekApi, "authenticate", reject)
    tentacle = _tentacle(FakeConversationManager())

    with pytest.raises(RuntimeError, match="launch token rejected"):
        await tentacle.__aenter__()

    assert FakeDeepseekProcess.stopped == 1


async def test_start_failure_never_attaches_to_another_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset()
    FakeDeepseekProcess.fail_start = True
    tentacle = _tentacle(FakeConversationManager())

    with pytest.raises(RuntimeError, match="exited before reporting a URL"):
        await tentacle.__aenter__()

    assert tentacle.process is None
    assert not calls_of("settings/describe")
    assert FakeDeepseekProcess.stopped == 0


@pytest.mark.parametrize("detach", ["cancel", "prompt", "close", "scope"])
async def test_detached_run_collects_through_turn_end(
    monkeypatch: pytest.MonkeyPatch, detach: str
) -> None:
    patch_gateway(monkeypatch)
    script = turn_events("finished")
    FakeDeepseekApi.reset(script[:2])
    conversations = FakeConversationManager()
    tentacle = _tentacle(conversations)
    prompted = asyncio.Event()
    accept = asyncio.Event()
    observed = asyncio.Event()
    close = asyncio.Event()
    scopes: list[anyio.CancelScope] = []
    discarded: list[ChatWorkspace] = []
    original_call = FakeDeepseekApi.call
    original_discard = tentacle.octomate.workspaces.discard

    async def call(
        client: FakeDeepseekApi, method: str, payload: JsonValue
    ) -> RpcResult:
        result = await original_call(client, method, payload)
        if method == "session/prompt":
            prompted.set()
            if detach == "prompt":
                await accept.wait()
        return result

    async def discard(workspace: ChatWorkspace) -> None:
        discarded.append(workspace)
        await original_discard(workspace)

    async def consume() -> None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            if detach in {"cancel", "prompt"}:
                await tentacle.run("go", conversation_address=KEY, thread_id=_THREAD)
                return
            async with tentacle.run_stream_events(
                "go", conversation_address=KEY, thread_id=_THREAD
            ) as stream:
                async for _ in stream:
                    observed.set()
                    if detach == "close":
                        await close.wait()
                        break

    monkeypatch.setattr(FakeDeepseekApi, "call", call)
    monkeypatch.setattr(tentacle.octomate.workspaces, "discard", discard)
    async with tentacle:
        task = asyncio.create_task(consume())
        try:
            async with asyncio.timeout(2):
                await prompted.wait()
                if detach in {"close", "scope"}:
                    await observed.wait()
            if detach in {"cancel", "prompt"}:
                task.cancel()
            elif detach == "scope":
                scopes[0].cancel()
            else:
                close.set()
            await asyncio.sleep(0)
            if detach in {"cancel", "prompt"}:
                task.cancel()
            await asyncio.sleep(0)

            assert not task.done()
            assert tentacle.driven_sessions == {"sess-1": 1}
            assert "sess-1" in tentacle.subscribers
            assert "sess-1" in tentacle.bridge_contexts
            assert len(tentacle.run_tasks) == 1
            assert tentacle.mux_task is not None
            assert not tentacle.mux_task.done()
            assert not calls_of("session/cancel")
            assert discarded == []
            assert conversations.runs == []
        finally:
            accept.set()
            # Outlive the observer's buffer, then commit the assistant message.
            FakeDeepseekApi.push("sess-1", [script[1]] * 150 + script[2:])
            async with asyncio.timeout(2):
                if detach in {"cancel", "prompt"}:
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    await task

        assert tentacle.driven_sessions == {}
        assert tentacle.subscribers == {}
        assert tentacle.bridge_contexts == {}
        assert tentacle.run_tasks == set()
        assert len(discarded) == 1
        assert not calls_of("session/cancel")
        [recorded] = conversations.runs
        assert any(
            isinstance(part, TextPart) and part.content == "finished"
            for message in recorded[2]
            for part in message.parts
        )


async def test_concurrent_run_waits_before_claiming_the_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    script = turn_events()
    FakeDeepseekApi.reset(script[:2])
    conversations = FakeConversationManager()
    tentacle = _tentacle(conversations)
    conversation = await conversations.ensure(_THREAD, agent_tentacle_id="deepseek")
    claimed: list[ChatWorkspace] = []
    first_prompted = asyncio.Event()
    second_waiting = asyncio.Event()
    claim_workspace = tentacle.octomate.workspaces.claim
    lock = asyncio.Lock()
    acquire = lock.acquire

    async def claim(workspace: ChatWorkspace) -> None:
        claimed.append(workspace)
        await claim_workspace(workspace)

    async def acquire_lock() -> bool:
        if lock.locked():
            second_waiting.set()
        return await acquire()

    async def consume() -> None:
        async with tentacle.run_stream_events(
            "first", conversation_address=KEY, thread_id=_THREAD
        ) as stream:
            async for _ in stream:
                first_prompted.set()

    monkeypatch.setattr(tentacle.octomate.workspaces, "claim", claim)
    monkeypatch.setattr(lock, "acquire", acquire_lock)
    tentacle.conversation_locks.by_session[str(conversation.id)] = lock
    async with tentacle:
        first = asyncio.create_task(consume())
        async with asyncio.timeout(2):
            await first_prompted.wait()
        FakeDeepseekApi.turn_script = script
        second = asyncio.create_task(
            tentacle.run("second", conversation_address=KEY, thread_id=_THREAD)
        )
        try:
            async with asyncio.timeout(2):
                await second_waiting.wait()
            assert len(claimed) == 1
            assert len(calls_of("session/prompt")) == 1
            assert not calls_of("session/cancel")
        finally:
            FakeDeepseekApi.push("sess-1", script[2:])
            async with asyncio.timeout(2):
                await first
                await second

        assert len(claimed) == 2
        assert len(conversations.runs) == 2


@pytest.mark.parametrize("cancel_shutdown", [False, True])
async def test_aexit_drains_live_sessions_before_closing_the_mux(
    monkeypatch: pytest.MonkeyPatch,
    cancel_shutdown: bool,
) -> None:
    patch_gateway(monkeypatch)
    script = turn_events()
    FakeDeepseekApi.reset(script[:2])
    conversations = FakeConversationManager()
    tentacle = _tentacle(conversations)
    observed = asyncio.Event()

    async def consume() -> None:
        async with tentacle.run_stream_events(
            "go", conversation_address=KEY, thread_id=_THREAD
        ) as stream:
            async for _ in stream:
                observed.set()

    await tentacle.__aenter__()
    task = asyncio.create_task(consume())
    async with asyncio.timeout(2):
        await observed.wait()
    shutdown = asyncio.create_task(tentacle.__aexit__())
    try:
        await asyncio.sleep(0)
        if cancel_shutdown:
            shutdown.cancel()
            await asyncio.sleep(0)
        assert not shutdown.done()
        assert not tentacle.closing
        assert tentacle.driven_sessions == {"sess-1": 1}
        assert "sess-1" in tentacle.subscribers
        assert "sess-1" in tentacle.bridge_contexts
        assert tentacle.mux_task is not None
        assert not tentacle.mux_task.done()
        assert not calls_of("session/cancel")
        assert FakeDeepseekProcess.stopped == 0
    finally:
        FakeDeepseekApi.push("sess-1", script[2:])
        async with asyncio.timeout(2):
            await task
            if cancel_shutdown:
                with pytest.raises(asyncio.CancelledError):
                    await shutdown
            else:
                await shutdown

    assert len(conversations.runs) == 1
    assert not calls_of("session/cancel")
    assert FakeDeepseekProcess.stopped == 1
    assert tentacle.mux_task is None
    assert tentacle.process is None
    assert tentacle.driven_sessions == {}


async def test_rejected_interaction_reply_fails_its_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeDeepseekApi.reset()
    tentacle = _tentacle(FakeConversationManager())
    api = FakeDeepseekApi(HttpUrl("http://t"), None)
    tentacle.client = cast(DeepseekApiClient, api)
    queue: asyncio.Queue[
        SessionEventFrame | SessionAssistantFrame | StreamErrorFrame
    ] = asyncio.Queue()
    tentacle.subscribers["sess-1"] = queue

    async def refuse(event_id: str, result: RpcResult | None) -> RpcReceipt:
        return RpcReceipt(accepted=False, reason="HTTP 500")

    monkeypatch.setattr(api, "respond", refuse)
    await tentacle.answer_interaction(
        "event-1",
        ApprovalRequestedFrame(
            type="approval/requested",
            session_id="sess-1",
            approval_id="approval-1",
            tool_name="bash",
        ),
    )
    frame = queue.get_nowait()
    assert isinstance(frame, StreamErrorFrame)
    assert frame.error.code == "interaction-reply-failed"
    assert "HTTP 500" in frame.error.message
