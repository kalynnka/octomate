"""`DeepseekTentacle` drives one fake `/api` gateway: the process and client are
monkeypatched fakes, the mux stream is an in-memory queue the fake feeds when
`session.prompt` (or `respond`) is called — mirroring the real gateway, where
prompting is what makes a turn's events flow."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, cast

import pytest
from pydantic import HttpUrl
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import PartStartEvent

from octomate import Octomate
from octomate.config import ChannelConfig
from octomate.config.agents import DeepseekConfig
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.tentacles.agents.deepseek import DeepseekTentacle
from octomate.tentacles.agents.deepseek import base as deepseek_base
from octomate.tentacles.agents.deepseek.client import DeepseekApiClient
from octomate.tentacles.agents.deepseek.wire import (
    ApprovalRequestedFrame,
    ErrResult,
    MuxFrame,
    OkResult,
    QuestionRequestedFrame,
    RpcError,
    RpcReceipt,
    RpcResult,
    SessionEventFrame,
    StreamErrorFrame,
)
from octomate.tentacles.channels.base import ChannelTentacle
from octomate.types.json import JsonObject, JsonValue
from tests.support.managers import (
    FakeConversation,
    FakeConversationManager,
    FakePresentedBatch,
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
    # When True, the spawn loses the bind race: the endpoint port comes up
    # serving (the winner's harness) and the start itself dies on the address.
    fail_start: ClassVar[bool] = False

    def __init__(
        self,
        *,
        executable: str,
        port: int,
        extra_args: list[str],
        dsh_home: Path,
        ready_timeout: float,
    ) -> None:
        self.executable = executable
        self.port = port
        self.extra_args = extra_args
        self.dsh_home = dsh_home
        self.ready_timeout = ready_timeout

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
    answers `host.describe` — empty until a fake process starts one, so the
    attach probe finds nothing and the tentacle takes the launch path unless a
    test adds the endpoint. `turn_script` frames flow when `session.prompt`
    is called; `after_respond` frames flow when `respond` is — how the real
    gateway behaves around a blocking approval."""

    serving: ClassVar[set[str]] = set()
    results: ClassVar[dict[str, RpcResult]] = {}
    calls: ClassVar[list[tuple[str, JsonValue]]] = []
    responds: ClassVar[list[tuple[str, RpcResult]]] = []
    turn_script: ClassVar[list[TurnEntry]] = []
    after_respond: ClassVar[list[TurnEntry]] = []
    outbox: ClassVar[asyncio.Queue[tuple[str, MuxFrame]] | None] = None
    last_session: ClassVar[str | None] = None
    frame_serial: ClassVar[int] = 0

    def __init__(self, base_url: HttpUrl, http_client: object) -> None:
        self.base_url = base_url
        self.http_client = http_client

    async def __aenter__(self) -> FakeDeepseekApi:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def answering(self) -> bool:
        return not isinstance(await self.call("host.describe", {}), ErrResult)

    @classmethod
    def reset(cls, turn_script: list[TurnEntry] | None = None) -> None:
        cls.serving = set()
        cls.results = {
            "host.describe": OkResult(value={}),
            "session.create": OkResult(value={"sessionId": "sess-1"}),
            "session.selectModel": OkResult(value={"selected": {}}),
            "session.prompt": OkResult(value={"accepted": True}),
            "session.cancel": OkResult(value={"accepted": True}),
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
            method == "host.describe"
            and str(self.base_url).rstrip("/") not in FakeDeepseekApi.serving
        ):
            return ErrResult(
                error=RpcError(code="internal", message=f"nothing at {self.base_url}")
            )
        if method == "session.prompt" and isinstance(payload, dict):
            session_id = cast(str, payload["sessionId"])
            FakeDeepseekApi.last_session = session_id
            FakeDeepseekApi.push(session_id, FakeDeepseekApi.turn_script)
        return FakeDeepseekApi.results[method]

    async def remote(self, endpoint: str, args: JsonObject) -> RpcResult:
        FakeDeepseekApi.calls.append((endpoint, {"args": args}))
        return FakeDeepseekApi.results[endpoint]

    async def respond(self, rpc_id: str, result: RpcResult) -> RpcReceipt:
        FakeDeepseekApi.responds.append((rpc_id, result))
        if FakeDeepseekApi.after_respond and FakeDeepseekApi.last_session is not None:
            FakeDeepseekApi.push(
                FakeDeepseekApi.last_session, FakeDeepseekApi.after_respond
            )
            FakeDeepseekApi.after_respond = []
        return RpcReceipt(accepted=True)

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

    async def present_actions(
        self, *, requests: object, **_: object
    ) -> FakePresentedBatch:
        self.requests.append(requests)
        return self.batch


@dataclass
class FakeChannel:
    feelers: FakeFeelers
    config: ChannelConfig = field(
        default_factory=lambda: ChannelConfig(type="fake", agents=[])
    )


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


async def wait_for_pending(tentacle: DeepseekTentacle) -> uuid.UUID:
    for _ in range(10000):
        if tentacle.pending:
            return next(iter(tentacle.pending))
        await asyncio.sleep(0)
    raise AssertionError("no dsh deferred batch was parked")


def interaction_octomate(
    feelers: FakeFeelers,
    deferred_actions: RecordingDeferredActions,
    conversations: FakeConversationManager | None = None,
) -> Octomate:
    return Octomate(
        conversations=conversations or FakeConversationManager(),
        deferred_actions=cast(DeferredActionManager, deferred_actions),
        channels=cast(dict[str, ChannelTentacle], {"im": FakeChannel(feelers=feelers)}),
    )


async def test_run_stream_events_creates_session_proxies_events_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events("done"))
    conversations = FakeConversationManager()
    tentacle = _tentacle(conversations)

    events = []
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

    [create_payload] = calls_of("session.create")
    assert create_payload == {"cwd": str(Path(".").absolute())}
    [select_payload] = calls_of("session.selectModel")
    assert select_payload == {
        "sessionId": "sess-1",
        "provider": "deepseek-official",
        "model": "deepseek-v4-pro",
        "reasoningEffort": "max",
    }
    [permission_payload] = calls_of("commands/execute")
    assert permission_payload == {
        "args": {"agentId": "sess-1", "line": "/permission workspace-write"}
    }
    [prompt_payload] = calls_of("session.prompt")
    assert prompt_payload == {
        "sessionId": "sess-1",
        "mode": "queue",
        "content": [{"type": "text", "text": "fix it"}],
    }

    [recorded] = conversations.runs
    fake, _label, messages = recorded
    assert fake.external_id == "sess-1"
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
    assert not calls_of("session.create")
    [prompt_payload] = calls_of("session.prompt")
    assert isinstance(prompt_payload, dict)
    assert prompt_payload["sessionId"] == "sess-old"


async def test_agent_preset_and_configured_cwd_reach_session_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events())
    tentacle = _tentacle(
        FakeConversationManager(),
        config=DeepseekConfig(cwd="/repo", agent_preset="octopus"),
    )

    async with tentacle:
        await tentacle.run("go", conversation_address=KEY, thread_id=_THREAD)

    [create_payload] = calls_of("session.create")
    assert create_payload == {"cwd": "/repo", "agentPreset": "octopus"}


async def test_without_a_model_the_session_selection_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events())
    tentacle = _tentacle(FakeConversationManager())

    async with tentacle:
        await tentacle.run("go", conversation_address=KEY, thread_id=_THREAD)

    assert not calls_of("session.selectModel")


async def test_the_conversations_posture_overrides_the_configured_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events())
    conversations = FakeConversationManager()
    conversations.store[(_THREAD, "deepseek", "")] = FakeConversation(
        thread_id=_THREAD, permission_mode="danger-full-access"
    )
    tentacle = _tentacle(conversations)

    async with tentacle:
        await tentacle.run("go", conversation_address=KEY, thread_id=_THREAD)

    [permission_payload] = calls_of("commands/execute")
    assert isinstance(permission_payload, dict)
    assert permission_payload["args"] == {
        "agentId": "sess-1",
        "line": "/permission danger-full-access",
    }


async def test_a_wrong_provider_posture_falls_back_to_config(
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
        await tentacle.run("go", conversation_address=KEY, thread_id=_THREAD)

    [permission_payload] = calls_of("commands/execute")
    assert isinstance(permission_payload, dict)
    assert permission_payload["args"] == {
        "agentId": "sess-1",
        "line": "/permission workspace-write",
    }


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

    [prompt_payload] = calls_of("session.prompt")
    assert isinstance(prompt_payload, dict)
    assert prompt_payload["content"] == [
        {"type": "text", "text": "You are an accomplice.\n\nwork the brief"}
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


async def test_a_slash_intercepted_prompt_is_the_whole_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset()
    FakeDeepseekApi.results["session.prompt"] = OkResult(
        value={
            "accepted": True,
            "command": {"kind": "success", "text": "preset workspace-write"},
        }
    )
    conversations = FakeConversationManager()
    tentacle = _tentacle(conversations)

    async with tentacle:
        result = await tentacle.run(
            "/permission workspace-write",
            conversation_address=KEY,
            thread_id=_THREAD,
        )

    assert result.output == "preset workspace-write"
    assert not calls_of("session.cancel")
    [recorded] = conversations.runs
    _fake, _label, messages = recorded
    assert messages


async def test_a_refused_rpc_becomes_the_runs_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events())
    FakeDeepseekApi.results["session.create"] = ErrResult(
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
    [cancel_payload] = calls_of("session.cancel")
    assert cancel_payload == {"sessionId": "sess-1"}


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
    assert not calls_of("session.cancel")


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
        batch_id = await wait_for_pending(tentacle)
        await octomate.kick(
            DeferredActionBatchResponse(
                batch_id=batch_id, approvals={approval.id: True}
            )
        )
        result = await run

    assert result.output == "released"
    assert len(feelers.requests) == 1
    [(rpc_id, response)] = FakeDeepseekApi.responds
    assert isinstance(response, OkResult)
    assert response.value == {
        "sessionId": "sess-1",
        "approvalId": "ap-1",
        "outcome": "allowed-once",
    }


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
    batch_id = await wait_for_pending(tentacle)
    await octomate.kick(
        DeferredActionBatchResponse(batch_id=batch_id, approvals={approval.id: False})
    )
    await task

    [(rpc_id, response)] = FakeDeepseekApi.responds
    assert rpc_id == "rpc-9"
    assert isinstance(response, OkResult)
    assert isinstance(response.value, dict)
    assert response.value["outcome"] == "rejected"


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
    batch_id = await wait_for_pending(tentacle)
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
        cast(dict[str, JsonValue], response.value)["outcome"]
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
    assert isinstance(response.value, dict)
    assert response.value["outcome"] == "rejected"


async def test_a_request_nobody_drives_is_cancelled_not_left_hanging() -> None:
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
    assert isinstance(response, ErrResult)
    assert response.error.code == "cancelled"


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
    batch_id = await wait_for_pending(tentacle)
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
        "sessionId": "sess-1",
        "answer": {
            "answers": [
                {"id": "q1", "selected": ["main"]},
                {"id": "q2", "selected": [], "custom": "ship it"},
            ]
        },
    }


async def test_attaches_to_a_dsh_already_serving_the_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events("shared"))
    FakeDeepseekApi.serving.add("http://127.0.0.1:3080")
    tentacle = _tentacle(FakeConversationManager())

    async with tentacle:
        assert tentacle.process is None
        assert tentacle.client is not None
        assert tentacle.client.base_url == HttpUrl("http://127.0.0.1:3080")
        result = await tentacle.run("go", conversation_address=KEY, thread_id=_THREAD)

    assert result.output == "shared"
    assert not FakeDeepseekProcess.started
    # The attached harness is not ours to stop.
    assert FakeDeepseekProcess.stopped == 0


async def test_nothing_serving_starts_a_dsh_on_the_configured_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset()
    tentacle = _tentacle(FakeConversationManager(), config=DeepseekConfig(port=4090))

    async with tentacle:
        pass

    [process] = FakeDeepseekProcess.started
    assert process.port == 4090
    assert FakeDeepseekProcess.stopped == 1


async def test_losing_the_start_race_attaches_to_the_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset()
    FakeDeepseekProcess.fail_start = True
    tentacle = _tentacle(FakeConversationManager())

    async with tentacle:
        assert tentacle.process is None
        assert tentacle.client is not None
        assert tentacle.client.base_url == HttpUrl("http://127.0.0.1:3080")

    assert FakeDeepseekProcess.stopped == 0


async def test_aexit_cancels_live_sessions_and_stops_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset()
    tentacle = _tentacle(FakeConversationManager())

    async with tentacle:
        tentacle.subscribers["sess-9"] = asyncio.Queue()

    [cancel_payload] = calls_of("session.cancel")
    assert cancel_payload == {"sessionId": "sess-9"}
    assert FakeDeepseekProcess.stopped == 1
    assert tentacle.mux_task is None
    assert tentacle.process is None
