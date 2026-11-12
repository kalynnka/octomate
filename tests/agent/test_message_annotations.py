from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    AgentMessageThreadItem,
)
from pydantic_ai.messages import (
    EnqueuedMessagesEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextContent,
    TextPart,
    UserPromptPart,
)
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.capabilities.harness.events import StreamEvents, wire_event_adapter
from octomate.nerve import Impulse
from octomate.schemas.annotations import MessageAnnotations
from octomate.schemas.messages import ModelRequest, ModelResponse
from octomate.schemas.segments import MarkdownSegment, TextSegment
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.claude.content import prompt_content as claude_prompt
from octomate.tentacles.codex.adapter import CodexRunAccumulator
from octomate.tentacles.codex.content import answer_part
from octomate.tentacles.codex.content import prompt_content as codex_prompt
from octomate.tentacles.feelers.output import render_stream_event_delta
from tests.agent.claude import test_tailer as claude
from tests.agent.codex import test_native_ingest as codex
from tests.agent.codex.test_user_prompts import user_item
from tests.support.managers import a_loaded_thread
from tests.support.nerve import held

IDE = "# Context from my IDE setup:\n\n## Active file: src/main.py\n\n## Active selection of the file:\nrun()\n\n## Open tabs:\n- main.py: src/main.py\n- My file.py: src/My file.py\n\n## My request:\nlaunch a PR.\n"
CLAUDE = "<ide_opened_file>The user opened the file /work/main.py in the IDE. This may or may not be related to the current task.</ide_opened_file>\n<browser_instruction># Claude in Chrome browser automation\nUse browser tools.</browser_instruction>\nMy actual request."
CITATION = "<oai-mem-citation>\n<citation_entries>\nMEMORY.md:274-275|note=[Verified against current code]\n</citation_entries>\n<rollout_ids>\n01a1863b-63d4-78d1-9a53-944134fc9f01\n</rollout_ids>\n</oai-mem-citation>"
MIGRATION = ScriptDirectory.from_config(
    Config(str(Path(__file__).resolve().parents[2] / "octomate/migrations/alembic.ini"))
).get_revision("90e954ec0b2d")


def test_codex_context_keeps_the_request_and_deduplicates_file_roles() -> None:
    content = codex_prompt(IDE)
    assert isinstance(content, TextContent)
    result = MessageAnnotations.model_validate(content.metadata["annotations"])
    assert result is not None
    assert content.content == "launch a PR."
    assert [(ref.path, ref.roles) for ref in result.references] == [
        ("src/main.py", ["Active file", "Selection", "Open tab"]),
        ("src/My file.py", ["Open tab"]),
    ]
    assert "run()" in result.contexts[0].text


def test_claude_context_retains_injections_separately() -> None:
    content = claude_prompt(CLAUDE)
    assert isinstance(content, TextContent)
    result = MessageAnnotations.model_validate(content.metadata["annotations"])
    assert result is not None
    assert content.content == "My actual request."
    assert [context.title for context in result.contexts] == [
        "Opened file",
        "Browser instructions",
    ]
    assert result.references[0].path == "/work/main.py"


def test_claude_selected_lines_remain_a_reference() -> None:
    content = claude_prompt(
        "<ide_selection>The user selected the lines 35 to 38 from /work/nerve.py:\nselected code</ide_selection>\nExplain it."
    )
    assert isinstance(content, TextContent)
    result = MessageAnnotations.model_validate(content.metadata["annotations"])
    reference = result.references[0]
    assert (reference.path, reference.start_line, reference.end_line) == (
        "/work/nerve.py",
        35,
        38,
    )
    assert content.content == "Explain it."


@pytest.mark.parametrize(
    "text",
    [
        "Ordinary text",
        "```\n" + IDE + "```",
        "Quoted: " + CLAUDE,
        "# Context from my IDE setup:\nno request boundary",
        "<ide_opened_file>literal example</ide_opened_file>",
        "<browser_instruction>my own instructions</browser_instruction>",
        "<ide_selection>unfinished",
    ],
)
def test_literal_unknown_and_incomplete_prompts_are_unchanged(text: str) -> None:
    assert codex_prompt(text) == text
    assert claude_prompt(text) == text


def test_memory_sources_keep_lines_notes_and_rollouts() -> None:
    content = answer_part("Answer.\n\n" + CITATION)
    assert content.provider_details is not None
    result = MessageAnnotations.model_validate(content.provider_details["annotations"])
    assert result is not None
    assert content.content == "Answer."
    reference = result.references[0]
    assert (reference.path, reference.start_line, reference.end_line) == (
        "MEMORY.md",
        274,
        275,
    )
    assert reference.note == "Verified against current code"
    assert result.contexts[0].rollout_ids == ["01a1863b-63d4-78d1-9a53-944134fc9f01"]


@pytest.mark.parametrize(
    "text",
    [
        "`" + CITATION + "`",
        "```xml\n" + CITATION + "\n```",
        "~~~xml\n" + CITATION + "\n~~~",
        "> " + CITATION,
        CITATION + "\nThis is an example.",
        CITATION.replace("274-275", "275-274"),
        CITATION.replace("274-275", "bad"),
        CITATION.removesuffix("</oai-mem-citation>"),
    ],
)
def test_literal_and_malformed_citations_remain_visible(text: str) -> None:
    assert answer_part(text).content == text
    assert answer_part(text).provider_details is None


def citation_events(
    text: str, *, chunks: int = 1, accumulator: CodexRunAccumulator | None = None
) -> Iterator[StreamEvents[str]]:
    accumulator = accumulator if accumulator is not None else CodexRunAccumulator()
    for index in range(0, len(text), chunks):
        yield from accumulator.consume_agent_message_delta(
            AgentMessageDeltaNotification(
                thread_id="session",
                turn_id="turn",
                item_id="answer",
                delta=text[index : index + chunks],
            ),
            {},
        )
    yield from accumulator.complete_agent_message(
        AgentMessageThreadItem(id="answer", type="agentMessage", text=text), {}
    )


@pytest.mark.parametrize("chunks", [1, 7, 2048])
def test_live_wire_payload_and_stored_parts_match_without_channel_processing(
    chunks: int,
) -> None:
    accumulator = CodexRunAccumulator()
    raw = "Answer.\n\n" + CITATION
    displayed = ""
    final = None
    for event in citation_events(raw, chunks=chunks, accumulator=accumulator):
        # Both the console's wire reader and plain IM renderer get clean text.
        assert isinstance(event, (PartStartEvent, PartDeltaEvent, PartEndEvent))
        event = wire_event_adapter.validate_json(wire_event_adapter.dump_json(event))
        if isinstance(event, (PartStartEvent, PartDeltaEvent, PartEndEvent)):
            delta = render_stream_event_delta(event)
            if delta is not None:
                displayed += delta.text
        assert "<oai" not in displayed
        if isinstance(event, PartEndEvent):
            final = event.part
    assert displayed.strip() == "Answer."
    assert isinstance(final, TextPart)
    assert final.content == accumulator.result_text == "Answer."
    assert final == accumulator.messages[-1].parts[0]
    assert final.provider_details is not None
    annotations = MessageAnnotations.model_validate(
        final.provider_details["annotations"]
    )
    assert annotations.raw == raw
    assert annotations.references[0].path == "MEMORY.md"


@pytest.mark.parametrize(
    "text",
    [
        "An ordinary answer.",
        "```xml\n" + CITATION + "\n```",
        "Answer.\n" + CITATION.removesuffix("</oai-mem-citation>"),
    ],
)
def test_literal_or_incomplete_streams_restore_all_text(text: str) -> None:
    accumulator = CodexRunAccumulator()
    events = list(citation_events(text, accumulator=accumulator))
    assert (
        "".join(
            delta.text
            for event in events
            if isinstance(event, (PartStartEvent, PartDeltaEvent, PartEndEvent))
            and (delta := render_stream_event_delta(event)) is not None
        )
        == text
    )
    part = accumulator.messages[-1].parts[0]
    assert isinstance(part, TextPart)
    assert part.content == text


@pytest.mark.usefixtures("in_memory_engine")
@pytest.mark.parametrize("separate_context", [False, True])
async def test_codex_ingest_persists_clean_prompt_reply_and_annotations(
    separate_context: bool,
) -> None:
    octomate = Octomate()
    _, tailer = codex.wired(octomate)
    send, receive = anyio.create_memory_object_stream[Impulse](math.inf)
    async with send, receive:
        state, _ = await tailer.attach_remote(
            codex.SESSION_ID,
            codex.ROLLOUT_LABEL,
            codex.SENDER,
            codex.SESSION_ADDRESS,
            send,
        )
        await codex.feed_records(
            tailer,
            state,
            [
                codex.event(0, "task_started", turn_id=codex.TURN_ID),
                *(
                    [
                        {
                            **user_item(
                                1,
                                "<external_ide_context>"
                                + IDE.partition("\n## My request:\n")[0]
                                + "</external_ide_context>",
                                kind="additional_content.ide_context",
                            )
                        }
                    ]
                    if separate_context
                    else []
                ),
                {**user_item(1, "launch a PR." if separate_context else IDE)},
                codex.answer_item(2, "Answer.\n\n" + CITATION),
                codex.event(3, "task_complete", turn_id=codex.TURN_ID),
            ],
        )
        tailer.detach_remote(state)
        live = [
            wire_event_adapter.validate_json(
                wire_event_adapter.dump_json(impulse.event)
            )
            for impulse in held(receive)
        ]
    [enqueued] = [event for event in live if isinstance(event, EnqueuedMessagesEvent)]
    prompt = enqueued.messages[0].parts[0]
    assert isinstance(prompt, UserPromptPart)
    assert not isinstance(prompt.content, str)
    content = prompt.content[0]
    assert isinstance(content, TextContent)
    assert content.content == "launch a PR."
    thread = await a_loaded_thread(
        octomate.threads, ThreadKey.from_address(codex.SESSION_ADDRESS)
    )
    assert [message.message_text for message in thread.messages] == [
        "launch a PR.",
        "Answer.",
    ]
    prompt_segment, answer_segment = [
        message.segments[0] for message in thread.messages
    ]
    assert isinstance(prompt_segment, TextSegment)
    assert isinstance(answer_segment, MarkdownSegment)
    assert "annotations" in prompt_segment.data
    assert "annotations" in answer_segment.data
    assert prompt_segment.data["annotations"].raw == (
        "launch a PR." if separate_context else IDE
    )
    assert prompt_segment.data["annotations"].references[0].path == "src/main.py"
    assert answer_segment.data["annotations"].references[0].path == "MEMORY.md"
    page = await octomate.conversations.messages_page(thread.id, limit=100)
    assert {message.message_text for message in page.items} == {
        "launch a PR.",
        "Answer.",
    }
    assert {
        part.content
        for message in page.items
        for part in message.parts
        if isinstance(part, TextPart)
    } == {"Answer."}
    stored_prompt = next(
        part
        for message in page.items
        for part in message.parts
        if isinstance(part, UserPromptPart)
    )
    assert stored_prompt == prompt
    stored_answer = next(
        part
        for message in page.items
        for part in message.parts
        if isinstance(part, TextPart)
    )
    live_answer = next(event.part for event in live if isinstance(event, PartEndEvent))
    assert stored_answer == live_answer


@pytest.mark.usefixtures("in_memory_engine")
async def test_claude_ingest_persists_clean_prompt_and_annotations() -> None:
    octomate = Octomate()
    _, tailer = claude.wired(octomate)
    await claude.stream_in(
        tailer,
        octomate.nerve,
        [
            claude.prompt_record("prompt", CLAUDE, 0),
            claude.assistant_record("reply", 1, [{"type": "text", "text": "Done."}]),
        ],
    )
    thread = await a_loaded_thread(octomate.threads, claude.SESSION_KEY)
    assert [message.message_text for message in thread.messages] == [
        "My actual request.",
        "Done.",
    ]
    segment = thread.messages[0].segments[0]
    assert isinstance(segment, TextSegment)
    assert "annotations" in segment.data
    assert segment.data["annotations"].raw == CLAUDE
    page = await octomate.conversations.messages_page(thread.id, limit=100)
    request = next(message for message in page.items if message.role == "user")
    assert request.message_text == "My actual request."
    part = request.parts[0]
    assert isinstance(part, UserPromptPart)
    assert not isinstance(part.content, str)
    content = part.content[0]
    assert isinstance(content, TextContent)
    assert (
        MessageAnnotations.model_validate(content.metadata["annotations"])
        == segment.data["annotations"]
    )


async def test_migration_cleans_unbound_ledger_and_preserves_raw_model_payloads(
    in_memory_engine: AsyncEngine,
) -> None:
    octomate = Octomate()
    inbound = await octomate.threads.record_native_prompt(
        codex.SESSION_ADDRESS,
        message_id="legacy",
        text=IDE,
        sender=codex.SENDER,
        timestamp=datetime.now(UTC),
    )
    thread = await a_loaded_thread(
        octomate.threads, ThreadKey.from_address(codex.SESSION_ADDRESS)
    )
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id="codex"
    )
    raw = "Answer.\n\n" + CITATION
    run = await octomate.conversations.record_external_run(
        conversation,
        str(uuid7()),
        messages=[
            ModelRequest(parts=[UserPromptPart(IDE)]),
            ModelResponse(parts=[TextPart(raw, provider_details={"kept": "value"})]),
        ],
        native_session_id="legacy",
        end_offset=100,
    )
    assert run is not None
    # Leave the inbound row unbound, as older imports did.
    await octomate.threads.bind_native_run(
        codex.SESSION_ADDRESS, run, prompts={}, answer=raw, sender=codex.SENDER
    )

    def migrate(connection: Connection) -> None:
        assert MIGRATION is not None
        with Operations.context(MigrationContext.configure(connection)):
            MIGRATION.module.upgrade()
            persisted = connection.exec_driver_sql(
                "SELECT parts, message_text FROM model_messages ORDER BY id"
            ).all()
            MIGRATION.module.downgrade()
            MIGRATION.module.upgrade()
            assert (
                connection.exec_driver_sql(
                    "SELECT parts, message_text FROM model_messages ORDER BY id"
                ).all()
                == persisted
            )

    async with in_memory_engine.begin() as connection:
        await connection.run_sync(migrate)
    thread = await a_loaded_thread(
        octomate.threads, ThreadKey.from_address(codex.SESSION_ADDRESS)
    )
    assert [row.message_text for row in thread.messages] == ["launch a PR.", "Answer."]
    assert thread.messages[0].id == inbound.id
    page = await octomate.conversations.messages_page(thread.id, limit=100)
    response = next(
        message for message in page.items if isinstance(message, ModelResponse)
    )
    part = response.parts[0]
    assert isinstance(part, TextPart)
    assert part.content == "Answer."
    assert part.provider_details is not None
    assert part.provider_details["kept"] == "value"
    assert (
        MessageAnnotations.model_validate(part.provider_details["annotations"]).raw
        == raw
    )
