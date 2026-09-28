"""The command boundary distinguishes runtime capabilities and preserves input."""

from pathlib import Path

import pytest
from pydantic import Field, TypeAdapter, ValidationError
from uuid_utils.compat import uuid7

from octomate.schemas.commands import (
    AgentCommandCatalog,
    CommandCatalog,
    CommandContext,
    CommandDescriptor,
    CommandError,
    CommandInvocation,
    CommandOutcome,
    CommandResult,
    LoadingCommandCatalog,
    ReadyCommandCatalog,
    UnavailableCommandCatalog,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import FileData, FileSegment, TextSegment
from octomate.types.json import JsonObject
from tests.support.agents import FakeAgent


class CodexCommandDescriptor(CommandDescriptor):
    path: Path = Field(description="The skill path used by the Codex runtime.")


class ClaudeCommandDescriptor(CommandDescriptor):
    aliases: list[str] = Field(description="Additional upstream command names.")


class DeepseekCommandDescriptor(CommandDescriptor):
    definition_id: str = Field(description="The registered DSH definition identity.")


@pytest.mark.parametrize("status", ["unsupported", "unavailable", "failed"])
def test_catalog_failure_states_survive_wire_roundtrip(status: str) -> None:
    catalog = TypeAdapter(CommandCatalog).validate_python(
        {"status": status, "message": "Reconnect the runtime to discover its commands."}
    )

    assert isinstance(catalog, UnavailableCommandCatalog)
    assert catalog.status == status
    assert (
        TypeAdapter(CommandCatalog).validate_json(catalog.model_dump_json()) == catalog
    )


def test_empty_and_loading_catalogs_are_distinct() -> None:
    adapter = TypeAdapter(CommandCatalog)
    empty = adapter.validate_python({"status": "ready", "entries": []})
    loading = adapter.validate_python({"status": "loading"})

    assert isinstance(empty, ReadyCommandCatalog)
    assert empty.entries == []
    assert isinstance(loading, LoadingCommandCatalog)
    with pytest.raises(ValidationError, match="entries"):
        adapter.validate_python({"status": "ready"})


def test_unified_catalog_retains_discovery_limitations() -> None:
    catalog = AgentCommandCatalog.model_validate(
        {
            "agent_id": "runtime",
            "catalog": {
                "status": "ready",
                "limitations": ["Only skills are discoverable; no native command API."],
                "entries": [
                    {
                        "id": "workspace/review",
                        "name": "review",
                        "description": "Review selected files.",
                        "argument_hint": "[files…]",
                        "accepts_attachments": True,
                    }
                ],
            },
        }
    )

    assert isinstance(catalog.catalog, ReadyCommandCatalog)
    assert catalog.catalog.limitations == [
        "Only skills are discoverable; no native command API."
    ]
    entry = catalog.catalog.entries[0]
    assert entry.id == "workspace/review"
    assert entry.argument_hint == "[files…]"
    assert entry.accepts_attachments
    assert AgentCommandCatalog.model_validate_json(catalog.model_dump_json()) == catalog


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
            aliases=["check"],
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
) -> None:
    catalog = AgentCommandCatalog(
        agent_id="runtime", catalog=ReadyCommandCatalog(entries=[entry])
    )

    assert isinstance(catalog.catalog, ReadyCommandCatalog)
    assert catalog.catalog.entries[0] is entry
    wire = catalog.model_dump(mode="json")
    assert wire["catalog"]["entries"][0] == entry.model_dump(mode="json")
    restored = type(entry).model_validate(wire["catalog"]["entries"][0])
    assert restored == entry

    # Channels need only the common fields; runtime rehydration uses the subtype.
    channel_view = AgentCommandCatalog.model_validate_json(catalog.model_dump_json())
    assert isinstance(channel_view.catalog, ReadyCommandCatalog)
    common = channel_view.catalog.entries[0]
    assert type(common) is CommandDescriptor
    assert common.id == entry.id
    assert common.argument_hint == entry.argument_hint
    assert common.accepts_attachments == entry.accepts_attachments


def test_duplicate_ids_are_rejected_but_duplicate_names_are_allowed() -> None:
    first = CommandDescriptor(
        id="project/review", name="review", description="Project."
    )
    second = CommandDescriptor(id="user/review", name="review", description="Personal.")

    assert len(ReadyCommandCatalog(entries=[first, second]).entries) == 2
    with pytest.raises(ValidationError, match="ids must be unique"):
        ReadyCommandCatalog(entries=[first, first])


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


def test_catalog_and_outcome_discriminators_reject_unknown_states() -> None:
    for adapter in (TypeAdapter(CommandCatalog), TypeAdapter(CommandOutcome)):
        with pytest.raises(ValidationError, match="union_tag_invalid"):
            adapter.validate_python({"status": "invented"})


async def test_default_hooks_are_unsupported_without_starting_a_turn(
    tmp_path: Path,
) -> None:
    agent = FakeAgent()
    user_id = uuid7()
    context = CommandContext(
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
    outcome = await agent.execute_command(
        context,
        CommandInvocation(command_id="unknown", arguments=" /raw "),
    )

    assert catalog.agent_id == agent.id
    assert catalog.catalog.status == "unsupported"
    assert isinstance(outcome, CommandError)
    assert outcome.status == "unsupported"
    assert agent.turns == []
    assert agent.streams == []
    assert not context.cwd.exists()
