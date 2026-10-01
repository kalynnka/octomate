"""The command boundary distinguishes runtime capabilities and preserves input."""

from contextlib import aclosing
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import Field, TypeAdapter, ValidationError
from uuid_utils.compat import uuid7

from octomate.base import Octomate
from octomate.schemas.commands import (
    CommandCatalog,
    CommandContext,
    CommandDescriptor,
    CommandError,
    CommandInvocation,
    CommandOutcome,
    CommandResult,
)
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.segments import FileData, FileSegment, TextSegment
from octomate.types.json import JsonObject
from tests.support.agents import FakeAgent


class CodexCommandDescriptor(CommandDescriptor, frozen=True):
    path: Path = Field(description="The skill path used by the Codex runtime.")


class ClaudeCommandDescriptor(CommandDescriptor, frozen=True):
    aliases: tuple[str, ...] = Field(description="Additional upstream command names.")


class DeepseekCommandDescriptor(CommandDescriptor, frozen=True):
    definition_id: str = Field(description="The registered DSH definition identity.")


@pytest.fixture
def context(tmp_path: Path) -> CommandContext:
    return CommandContext(
        agent_id="runtime",
        user_id=uuid7(),
        address=ChannelAddress("web", "thread", "chat", "user", "composer"),
        cwd=tmp_path,
        conversation=None,
    )


@pytest.mark.parametrize("status", ["unsupported", "unavailable", "failed"])
def test_catalog_failure_states_survive_wire_roundtrip(
    context: CommandContext, status: str
) -> None:
    catalog = CommandCatalog.model_validate(
        {
            "context": context,
            "status": status,
            "message": "Reconnect the runtime to discover its commands.",
        }
    )
    assert catalog.status == status
    assert CommandCatalog.model_validate_json(catalog.model_dump_json()) == catalog
    with pytest.raises(ValidationError, match="require a message"):
        CommandCatalog.model_validate({"context": context, "status": status})


def test_empty_and_loading_catalogs_are_distinct(context: CommandContext) -> None:
    empty = CommandCatalog(context=context, status="ready")
    loading = CommandCatalog(context=context, status="loading")
    assert empty.descriptors == loading.descriptors == set()
    assert empty.status != loading.status


def test_flat_catalog_retains_conversation_context_on_wire(
    context: CommandContext,
) -> None:
    context = replace(
        context,
        conversation=Conversation(
            thread_id=uuid7(),
            agent_tentacle_id=context.agent_id,
            external_id="native-session",
        ),
    )
    catalog = CommandCatalog(context=context, status="ready")
    wire = catalog.model_dump_json()
    restored = CommandCatalog.model_validate_json(wire)
    assert restored.context.matches(context)
    assert set(catalog.model_dump()) == {
        "context",
        "descriptors",
        "status",
        "message",
        "limitations",
    }


def test_catalog_snapshot_preserves_extensions_and_isolates_mutable_data(
    context: CommandContext,
) -> None:
    context = replace(
        context,
        conversation=Conversation(
            thread_id=uuid7(), agent_tentacle_id=context.agent_id
        ),
    )
    catalog = CommandCatalog(
        context=context,
        status="ready",
        limitations=["Skills only."],
        descriptors={
            ClaudeCommandDescriptor(
                id="review", name="review", description="Review", aliases=("check",)
            )
        },
    )
    snapshot = catalog.snapshot()
    descriptor = next(iter(snapshot.descriptors))
    assert isinstance(descriptor, ClaudeCommandDescriptor)
    snapshot.descriptors.clear()
    snapshot.limitations.clear()
    assert snapshot.context.conversation is not None
    snapshot.context.conversation.external_id = "changed"
    original = next(iter(catalog.descriptors))
    assert isinstance(original, ClaudeCommandDescriptor)
    assert original.aliases == ("check",)
    assert catalog.limitations == ["Skills only."]
    assert catalog.context.conversation is not None
    assert catalog.context.conversation.external_id is None


@pytest.mark.parametrize("status", ["loading", "unsupported", "unavailable", "failed"])
def test_nonready_catalogs_reject_descriptors(
    context: CommandContext, status: str
) -> None:
    with pytest.raises(ValidationError, match="only ready"):
        CommandCatalog.model_validate(
            {
                "context": context,
                "status": status,
                "message": "Not ready.",
                "descriptors": [
                    {"id": "review", "name": "review", "description": "Review."}
                ],
            }
        )


def test_unified_catalog_retains_discovery_limitations(context: CommandContext) -> None:
    catalog = CommandCatalog.model_validate(
        {
            "context": context,
            "status": "ready",
            "limitations": ["Only skills are discoverable; no native command API."],
            "descriptors": [
                {
                    "id": "workspace/review",
                    "name": "review",
                    "description": "Review selected files.",
                    "argument_hint": "[files…]",
                    "accepts_attachments": True,
                }
            ],
        }
    )
    assert catalog.limitations == [
        "Only skills are discoverable; no native command API."
    ]
    entry = next(iter(catalog.descriptors))
    assert entry.id == "workspace/review"
    assert entry.argument_hint == "[files…]"
    assert entry.accepts_attachments
    assert CommandCatalog.model_validate_json(catalog.model_dump_json()) == catalog


@pytest.mark.parametrize(
    "entry",
    [
        CodexCommandDescriptor(
            id="skill:review",
            name="review",
            description="Review changes.",
            path=Path("/workspace/skills/review/SKILL.md"),
        ),
        ClaudeCommandDescriptor(
            id="review",
            name="review",
            description="Review changes.",
            aliases=("check",),
        ),
        DeepseekCommandDescriptor(
            id="plan",
            name="plan",
            description="Enter or leave plan mode.",
            argument_hint="[off|message]",
            accepts_attachments=True,
            definition_id="@deepseek-ai/dsh-plan-mode",
        ),
    ],
)
def test_runtime_extensions_survive_catalog_serialization(
    entry: CommandDescriptor,
    context: CommandContext,
) -> None:
    catalog = CommandCatalog(context=context, status="ready", descriptors={entry})
    assert hash(entry) == hash(type(entry).model_validate(entry.model_dump()))
    with pytest.raises(ValidationError, match="frozen_instance"):
        # The rejected assignment is the input under test: descriptors are frozen.
        entry.description = "Changed after hashing"  # pyright: ignore[reportAttributeAccessIssue]
    assert next(iter(catalog.descriptors)) is entry
    wire = catalog.model_dump(mode="json")
    assert wire["descriptors"][0] == entry.model_dump(mode="json")
    restored = type(entry).model_validate(wire["descriptors"][0])
    assert restored == entry

    # Channels need only the common fields; runtime rehydration uses the subtype.
    channel_view = CommandCatalog.model_validate_json(catalog.model_dump_json())
    common = next(iter(channel_view.descriptors))
    assert type(common) is CommandDescriptor
    assert common.id == entry.id
    assert common.argument_hint == entry.argument_hint
    assert common.accepts_attachments == entry.accepts_attachments


def test_duplicate_ids_are_rejected_but_duplicate_names_are_allowed(
    context: CommandContext,
) -> None:
    first = CommandDescriptor(
        id="project/review", name="review", description="Project."
    )
    second = CommandDescriptor(id="user/review", name="review", description="Personal.")

    assert (
        len(
            CommandCatalog(
                context=context, status="ready", descriptors={first, second}
            ).descriptors
        )
        == 2
    )
    duplicate = CommandDescriptor.model_validate(first.model_dump())
    assert hash(first) == hash(duplicate)
    assert CommandCatalog(
        context=context, status="ready", descriptors={first, duplicate}
    ).descriptors == {first}
    conflict = first.model_copy(update={"description": "Conflicting definition."})
    with pytest.raises(ValidationError, match="ids must be unique"):
        CommandCatalog(context=context, status="ready", descriptors={first, conflict})


def test_catalog_deduplicates_wire_descriptors(context: CommandContext) -> None:
    descriptor = {"id": "review", "name": "review", "description": "Review."}
    catalog = CommandCatalog.model_validate(
        {
            "context": context,
            "status": "ready",
            "descriptors": [descriptor, descriptor],
        }
    )
    assert len(catalog.descriptors) == 1
    restored = CommandCatalog.model_validate_json(catalog.model_dump_json())
    assert restored.descriptors == catalog.descriptors


@pytest.mark.parametrize("accepts", [None, False, True])
def test_attachment_support_distinguishes_unknown_from_unsupported(
    accepts: bool | None,
) -> None:
    entry = CommandDescriptor(
        id="inspect",
        name="inspect",
        description="Inspect inputs.",
        accepts_attachments=accepts,
    )

    restored = CommandDescriptor.model_validate_json(entry.model_dump_json())
    assert restored.accepts_attachments is accepts
    assert (
        CommandDescriptor(id="plain", name="plain", description="").accepts_attachments
        is None
    )


def test_invocation_preserves_arguments_and_resolved_attachments() -> None:
    arguments = '  "a b"\n--flag=✓  '
    invocation = CommandInvocation(
        command_id="inspect",
        arguments=arguments,
        attachments=[FileSegment(data=FileData(file="/resolved/input.txt"))],
    )
    restored = CommandInvocation.model_validate_json(invocation.model_dump_json())

    assert restored.arguments == arguments
    assert isinstance(restored.attachments[0], FileSegment)
    assert restored.attachments[0].data.file == "/resolved/input.txt"


@pytest.mark.parametrize(
    "payload",
    [
        {"arguments": "missing selection"},
        {"command_id": ""},
        {
            "command_id": "inspect",
            "attachments": [{"type": "text", "data": {"text": "not an attachment"}}],
        },
    ],
)
def test_invocation_rejects_invalid_intent_and_attachment_shapes(
    payload: JsonObject,
) -> None:
    with pytest.raises(ValidationError):
        CommandInvocation.model_validate(payload)


@pytest.mark.parametrize(
    "status", ["unsupported", "unknown", "stale", "busy", "unavailable", "failed"]
)
def test_invocation_errors_remain_distinct(status: str) -> None:
    adapter = TypeAdapter(CommandOutcome)
    outcome = adapter.validate_python(
        {"status": status, "message": "Refresh and retry."}
    )

    assert isinstance(outcome, CommandError)
    assert outcome.status == status
    assert adapter.validate_json(outcome.model_dump_json()) == outcome


def test_direct_result_uses_existing_channel_segments() -> None:
    result = CommandResult(segments=[TextSegment(data={"text": "Control completed."})])
    restored = TypeAdapter(CommandOutcome).validate_json(result.model_dump_json())

    assert isinstance(restored, CommandResult)
    assert restored.segments == result.segments
    assert "run_id" not in restored.model_dump()


def test_catalog_and_outcome_reject_unknown_states(context: CommandContext) -> None:
    with pytest.raises(ValidationError, match="literal_error"):
        CommandCatalog.model_validate({"context": context, "status": "invented"})
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        TypeAdapter(CommandOutcome).validate_python({"status": "invented"})


async def test_default_hooks_are_unsupported_without_starting_a_turn(
    tmp_path: Path,
) -> None:
    app = Octomate()
    agent = app.connect(FakeAgent())
    agent.commands = app.commands
    user_id = uuid7()
    context = CommandContext(
        agent_id=agent.id,
        user_id=user_id,
        address=ChannelAddress(
            channel_tentacle_id="trunkline",
            chat_type="thread",
            chat_id=str(user_id),
            user_id=str(user_id),
            channel_thread_id="new-composer",
        ),
        cwd=tmp_path / "unprepared-workspace",
        conversation=None,
    )

    catalog = await agent.discover_commands(context)
    async with aclosing(
        agent.execute_command(
            context,
            CommandInvocation(command_id="unknown", arguments=" /raw "),
        )
    ) as events:
        outcome = await anext(events)

    assert catalog.context.agent_id == agent.id
    assert catalog.status == "unsupported"
    assert isinstance(outcome, CommandError)
    assert outcome.status == "unsupported"
    assert agent.turns == []
    assert agent.streams == []
    assert context.cwd is not None
    assert not context.cwd.exists()
