"""Native Codex prompt provenance, mixed formats, and replay."""

from datetime import UTC, datetime

import pytest
from anyio.abc import ObjectSendStream

from octomate import Octomate
from octomate.database import async_session
from octomate.managers.conversation import ConversationManager
from octomate.nerve import Impulse
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.codex.hooks import CodexHookInput
from octomate.tentacles.codex.tailer import OpenTurn
from octomate.tentacles.codex.transcript import RolloutLine, UserMessageIdentities
from octomate.types.json import JsonObject
from tests.agent.codex.test_native_ingest import (
    CHILD_THREAD_ID,
    CHILD_TURN_ONE,
    ROLLOUT_LABEL,
    SENDER,
    SESSION_ADDRESS,
    SESSION_ID,
    TURN_ID,
    answer_item,
    child_metadata,
    feed_records,
    parent_metadata,
    wired,
)
from tests.agent.codex.test_native_ingest import db as db
from tests.agent.codex.test_native_ingest import event as event
from tests.agent.codex.test_native_ingest import impulses as impulses
from tests.support.managers import a_loaded_thread


def user_item(
    second: int,
    text: str,
    *,
    kind: str = "user.text",
    turn_id: str = TURN_ID,
) -> JsonObject:
    return {
        "timestamp": f"2026-07-16T10:00:{second:02d}Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {
                "turn_id": turn_id,
                "content_item_kinds": [kind],
            },
        },
    }


@pytest.mark.parametrize("next_turn", [False, True])
def test_prompt_identity_pairing_distinguishes_repeated_text(next_turn: bool) -> None:
    identities = UserMessageIdentities()
    identities.consume(
        RolloutLine.model_validate(event(0, "task_started", turn_id=TURN_ID)),
        SESSION_ID,
    )
    expected: dict[str, str] = {}
    for index in range(2):
        turn_id = f"turn-{index}" if next_turn else TURN_ID
        if next_turn:
            identities.consume(
                RolloutLine.model_validate(event(0, "task_started", turn_id=turn_id)),
                SESSION_ID,
            )
        prompt = user_item(1, "identical prompt", turn_id=turn_id)
        payload = prompt["payload"]
        assert isinstance(payload, dict)
        payload["id"] = f"transcript-{index}"
        for record in (
            prompt,
            event(1, "user_message", message="identical prompt"),
            event(
                1,
                "item_completed",
                thread_id=SESSION_ID,
                turn_id=turn_id,
                item={"type": "UserMessage", "id": f"app-server-{index}"},
            ),
        ):
            identities.consume(RolloutLine.model_validate(record), SESSION_ID)
        expected[f"app-server-{index}"] = f"transcript-{index}"
    assert identities.aliases == expected


@pytest.mark.parametrize(
    "case",
    [
        "foreign_session",
        "foreign_turn",
        "injected",
        "ambiguous",
        "ended",
        "after_end",
        "missing_id",
    ],
)
def test_prompt_identity_pairing_requires_an_unambiguous_source_occurrence(
    case: str,
) -> None:
    identities = UserMessageIdentities()
    prompt = user_item(
        1,
        "identical prompt",
        kind="agents_md.instructions" if case == "injected" else "user.text",
    )
    payload = prompt["payload"]
    assert isinstance(payload, dict)
    if case != "missing_id":
        payload["id"] = "transcript-id"
    records = [event(0, "task_started", turn_id=TURN_ID), prompt]
    if case == "after_end":
        records.insert(1, event(0, "task_complete", turn_id=TURN_ID))
    elif case == "ambiguous":
        records.append(prompt)
    elif case == "ended":
        records.append(event(1, "task_complete", turn_id=TURN_ID))
    records.append(
        event(
            2,
            "item_completed",
            thread_id="foreign" if case == "foreign_session" else SESSION_ID,
            turn_id="foreign" if case == "foreign_turn" else TURN_ID,
            item={"type": "UserMessage", "id": "app-server-id"},
        )
    )
    for record in records:
        identities.consume(RolloutLine.model_validate(record), SESSION_ID)
    assert not identities.aliases


@pytest.mark.parametrize("format", ["current", "legacy", "both", "legacy-first"])
async def test_prompts_keep_order_identity_and_sender_on_replay(
    impulses: ObjectSendStream[Impulse], format: str
) -> None:
    octomate = Octomate()
    ingest, tailer = wired(octomate)
    await ingest.handle(
        CodexHookInput(
            hook_event_name="UserPromptSubmit",
            session_id=SESSION_ID,
            turn_id=TURN_ID,
            prompt="ask again",
        )
    )
    records = [event(0, "task_started", turn_id=TURN_ID)]
    for second in (1, 4):
        current = user_item(second, "ask again")
        legacy = event(second, "user_message", message="ask again")
        records.extend(
            [{**current}, legacy]
            if format == "both"
            else [legacy, {**current}]
            if format == "legacy-first"
            else [{**current}]
            if format == "current"
            else [legacy]
        )
        records.append(answer_item(second + 1, f"answer {second}"))
    records.append(event(6, "task_complete", turn_id=TURN_ID))
    for replay in (records[:-1], records, records):
        state, _ = await tailer.attach_remote(
            SESSION_ID, ROLLOUT_LABEL, SENDER, SESSION_ADDRESS, impulses
        )
        await feed_records(tailer, state, replay)
        tailer.detach_remote(state)
    thread = await a_loaded_thread(
        octomate.threads, ThreadKey("codex", "thread", SESSION_ID)
    )
    assert [row.message_text for row in thread.messages] == [
        "ask again",
        "ask again",
        "answer 4",
    ]
    inbound = [row for row in thread.messages if row.direction == "inbound"]
    assert inbound[0].platform_message_id == TURN_ID
    assert inbound[1].platform_message_id is not None
    assert inbound[1].platform_message_id.startswith(f"{TURN_ID}:")
    assert [row.happened_at for row in inbound] == [
        datetime(2026, 7, 16, 10, 0, second, tzinfo=UTC) for second in (1, 4)
    ]
    senders = [await row.sender for row in inbound]
    assert all(
        sender is not None and sender.channel_user_id == SENDER.channel_user_id
        for sender in senders
    )
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id="codex"
    )
    [run] = conversation.runs
    assert run.id == TURN_ID
    assert [message.message_text for message in run.messages] == [
        "ask again",
        "answer 1",
        "ask again",
        "answer 4",
    ]
    page = await octomate.conversations.messages_page(thread.id, limit=100)
    assert {
        row.platform_message_id
        for message in page.items
        for row in message.thread_messages
        if row.direction == "inbound"
    } == {row.platform_message_id for row in inbound}


@pytest.mark.parametrize(
    ("content", "kinds", "expected"),
    [
        (
            [
                {"type": "input_text", "text": "context"},
                {"type": "input_text", "text": "human text"},
                {"type": "input_image", "image_url": "data:image/png;base64,AA=="},
            ],
            ["agents_md.instructions", "user.text", "user.image"],
            "human text",
        ),
        (
            [
                {"type": "input_text", "text": "first"},
                {"type": "input_text", "text": "second"},
            ],
            ["user.text", "user.text"],
            "first\nsecond",
        ),
        ([{"type": "input_text", "text": "unlabelled"}], [], None),
        ([{"type": "input_text", "text": ""}], ["user.text"], None),
    ],
)
def test_only_aligned_human_text_parts_are_imported(
    content: list[JsonObject], kinds: list[str], expected: str | None
) -> None:
    line = RolloutLine.model_validate(
        {
            "timestamp": "2026-07-16T10:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": content,
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": TURN_ID,
                    "content_item_kinds": kinds,
                },
            },
        }
    )
    turn = OpenTurn(TURN_ID, 0, 0, line.timestamp, None)
    assert turn.consume_prompt(line) == expected


async def test_subagent_prompts_use_the_same_provenance_and_deduplication(
    impulses: ObjectSendStream[Impulse],
) -> None:
    octomate = Octomate()
    _, tailer = wired(octomate)
    state, _ = await tailer.attach_remote(
        SESSION_ID, ROLLOUT_LABEL, SENDER, SESSION_ADDRESS, impulses
    )
    await feed_records(tailer, state, [parent_metadata()])
    await feed_records(
        tailer,
        state,
        [
            child_metadata(),
            event(0, "task_started", turn_id=CHILD_TURN_ONE),
            {
                **user_item(
                    1,
                    "environment",
                    kind="environments.environment_context",
                    turn_id=CHILD_TURN_ONE,
                )
            },
            {**user_item(2, "child prompt", turn_id=CHILD_TURN_ONE)},
            event(2, "user_message", message="child prompt"),
            answer_item(3, "child answer"),
            event(4, "task_complete", turn_id=CHILD_TURN_ONE),
        ],
        key=f"rollout-child-{CHILD_THREAD_ID}.jsonl",
    )
    child = state.subagents[CHILD_THREAD_ID].conversation
    assert child is not None
    child = await octomate.conversations.get(child.id)
    [run] = child.runs
    assert run.id == CHILD_TURN_ONE
    assert [message.message_text for message in run.messages] == [
        "child prompt",
        "child answer",
    ]
    thread = await a_loaded_thread(
        octomate.threads, ThreadKey("codex", "thread", SESSION_ID)
    )
    assert not thread.messages


async def test_context_and_other_turns_are_not_human_prompts(
    impulses: ObjectSendStream[Impulse],
) -> None:
    octomate = Octomate()
    _, tailer = wired(octomate)
    state, _ = await tailer.attach_remote(
        SESSION_ID, ROLLOUT_LABEL, SENDER, SESSION_ADDRESS, impulses
    )
    await feed_records(
        tailer,
        state,
        [
            {**user_item(0, "outside a turn")},
            event(1, "task_started", turn_id=TURN_ID),
            {**user_item(2, "instructions", kind="agents_md.instructions")},
            {**user_item(3, "environment", kind="environments.environment_context")},
            {
                **user_item(
                    4, "open page", kind="additional_content.codex_apps_open_page"
                )
            },
            {**user_item(5, "wrong turn", turn_id="another-turn")},
            {**user_item(6, "actual prompt")},
            answer_item(7, "done"),
            event(8, "task_complete", turn_id=TURN_ID),
        ],
    )
    assert state.conversation is not None
    conversation = await octomate.conversations.get(state.conversation.id)
    assert [
        message.message_text
        for message in (await ConversationManager().history(conversation.id))
    ] == [
        "actual prompt",
        "done",
    ]


async def test_explicit_reimport_repairs_a_committed_turn_without_duplicating_reply(
    impulses: ObjectSendStream[Impulse],
) -> None:
    octomate = Octomate()
    _, tailer = wired(octomate)
    records = [
        event(0, "task_started", turn_id=TURN_ID),
        user_item(1, "missing prompt"),
        answer_item(2, "existing reply"),
        event(3, "task_complete", turn_id=TURN_ID),
    ]
    state, _ = await tailer.attach_remote(
        SESSION_ID, ROLLOUT_LABEL, SENDER, SESSION_ADDRESS, impulses
    )
    await feed_records(tailer, state, [records[0], *records[2:]])
    tailer.detach_remote(state)
    thread = await a_loaded_thread(
        octomate.threads, ThreadKey("codex", "thread", SESSION_ID)
    )
    [reply] = thread.messages
    for replay, repair in enumerate((False, True, False)):
        if repair:
            async with async_session() as session:
                run = await session.get(ExternalAgentRun, TURN_ID)
                assert run is not None
                run.end_offset = None
                await session.commit()
        state, _ = await tailer.attach_remote(
            SESSION_ID, ROLLOUT_LABEL, SENDER, SESSION_ADDRESS, impulses
        )
        await feed_records(tailer, state, records)
        tailer.detach_remote(state)
        thread = await a_loaded_thread(octomate.threads, thread.key)
        assert len(thread.messages) == (1 if replay == 0 else 2)
    assert [(row.direction, row.message_text) for row in thread.messages] == [
        ("inbound", "missing prompt"),
        ("outbound", "existing reply"),
    ]
    assert thread.messages[-1].id == reply.id
    assert state.conversation is not None
    conversation = await octomate.conversations.get(state.conversation.id)
    [run] = conversation.runs
    assert [message.message_text for message in run.messages] == [
        "missing prompt",
        "existing reply",
    ]
