"""Scoped discovery, bounded caching, and concurrent runtime changes."""

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

import pytest
from pydantic import Field
from uuid_utils.compat import uuid7

from octomate.base import Octomate
from octomate.managers.commands import CommandCatalogKey, CommandManager
from octomate.managers.conversation import ConversationManager
from octomate.managers.gateway import GatewayManager
from octomate.managers.thread import ThreadManager
from octomate.managers.user import UserManager
from octomate.managers.workspaces import WorkspaceManager
from octomate.schemas.commands import (
    CommandCatalog,
    CommandContext,
    CommandDescriptor,
)
from octomate.schemas.conversation import ChannelAddress, Conversation
from tests.support.agents import FakeAgent


class SkillDescriptor(CommandDescriptor, frozen=True):
    path: Path = Field(description="Runtime skill path.")


@dataclass
class DiscoveringAgent(FakeAgent):
    calls: list[CommandContext] = field(default_factory=list)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event | None = None
    failure: bool = False
    owner: str | None = None
    status: Literal["ready", "loading", "unsupported", "unavailable", "failed"] = (
        "ready"
    )
    descriptors: set[CommandDescriptor] = field(
        default_factory=lambda: {
            SkillDescriptor(
                id="skill", name="review", description="Review", path=Path("skill")
            )
        }
    )

    async def probe_commands(self, context: CommandContext) -> CommandCatalog:
        self.calls.append(context)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.failure:
            raise RuntimeError("private runtime details")
        return CommandCatalog(
            context=replace(context, agent_id=self.owner) if self.owner else context,
            status=self.status,
            descriptors=self.descriptors if self.status == "ready" else set(),
            message="Reason"
            if self.status in {"unsupported", "unavailable", "failed"}
            else None,
        )


@pytest.fixture
def manager() -> CommandManager:
    users = UserManager()
    return CommandManager(
        tentacles={},
        users=users,
        conversations=ConversationManager(),
        threads=ThreadManager(users=users),
        workspaces=WorkspaceManager(),
        gateway=GatewayManager(),
    )


@pytest.fixture
def context(tmp_path: Path) -> CommandContext:
    return CommandContext(
        agent_id="inkling",
        user_id=uuid7(),
        address=ChannelAddress("web", "thread", "chat", "user", "composer"),
        cwd=tmp_path / "not-created",
        conversation=Conversation(thread_id=uuid7(), agent_tentacle_id="inkling"),
    )


def test_command_manager_uses_the_hosts_registry_and_managers() -> None:
    app = Octomate()
    assert app.commands.tentacles is app.tentacles
    assert app.commands.users is app.users
    assert app.commands.conversations is app.conversations
    assert app.commands.threads is app.threads
    assert app.commands.workspaces is app.workspaces
    assert app.commands.gateway is app.gateway


async def test_cache_preserves_extensions_and_returns_independent_copies(
    manager: CommandManager,
    context: CommandContext,
) -> None:
    agent = DiscoveringAgent()
    first = await manager.discover(agent, context)
    first.descriptors.clear()
    second = await manager.discover(agent, context)
    descriptor = next(iter(second.descriptors))
    assert isinstance(descriptor, SkillDescriptor)
    assert descriptor.path == Path("skill")
    assert len(agent.calls) == 1
    assert agent.turns == []
    assert agent.streams == []
    assert context.cwd is not None
    assert not context.cwd.exists()
    assert context.conversation is not None
    assert list(manager.catalogs) == [
        CommandCatalogKey(context.user_id, context.agent_id, context.conversation.id)
    ]


@pytest.mark.parametrize(
    "scope",
    ["user", "address", "workspace", "model", "permission", "conversation", "session"],
)
async def test_context_changes_do_not_reuse_catalogs(
    manager: CommandManager, context: CommandContext, scope: str
) -> None:
    conversation = Conversation(thread_id=uuid7(), agent_tentacle_id="inkling")
    context = replace(context, conversation=conversation)
    match scope:
        case "user":
            changed = replace(context, user_id=uuid7())
        case "address":
            changed = replace(
                context, address=replace(context.address, user_id="other")
            )
        case "workspace":
            assert context.cwd is not None
            changed = replace(context, cwd=context.cwd / "other")
        case "model":
            changed = replace(context, model="other")
        case "permission":
            changed = replace(context, permission_mode="other")
        case "conversation":
            changed = replace(
                context,
                conversation=Conversation(
                    thread_id=conversation.thread_id, agent_tentacle_id="inkling"
                ),
            )
        case "session":
            changed = replace(
                context,
                conversation=conversation.model_copy(update={"external_id": "resumed"}),
            )
        case _:
            pytest.fail(f"unhandled scope {scope}")
    agent = DiscoveringAgent()
    await manager.discover(agent, context)
    await manager.discover(agent, changed)
    assert len(agent.calls) == 2
    assert len(manager.catalogs) == (2 if scope in {"user", "conversation"} else 1)


async def test_runtime_reconnect_invalidates_catalog(
    manager: CommandManager, context: CommandContext
) -> None:
    for agent in (DiscoveringAgent(), DiscoveringAgent()):
        manager.invalidate(agent_id=agent.id)
        await manager.discover(agent, context)
        assert len(agent.calls) == 1


@pytest.mark.parametrize("new_conversation", [False, True])
async def test_explicit_refresh_reads_changed_catalog(
    manager: CommandManager, context: CommandContext, new_conversation: bool
) -> None:
    if new_conversation:
        context = replace(context, conversation=None)
    agent = DiscoveringAgent()
    await manager.discover(agent, context)
    agent.descriptors = set()
    cached = await manager.discover(agent, context)
    assert cached.descriptors
    assert len(agent.calls) == 1
    refreshed = await manager.discover(agent, context, refresh=True)
    assert refreshed.descriptors == set()
    assert len(agent.calls) == 2


@pytest.mark.parametrize("status", ["loading", "failed", "unsupported", "unavailable"])
async def test_transient_states_are_reprobed(
    manager: CommandManager, context: CommandContext, status: str
) -> None:
    agent = DiscoveringAgent()
    agent.status = CommandCatalog.model_validate(
        {"context": context, "status": status, "message": "Reason"}
    ).status
    assert (await manager.discover(agent, context)).status == status
    await manager.discover(agent, context)
    assert len(agent.calls) == (2 if status in {"loading", "failed"} else 1)


@pytest.mark.parametrize("new_conversation", [False, True])
async def test_concurrent_refreshes_share_probe_and_caller_cancellation_is_local(
    manager: CommandManager,
    context: CommandContext,
    new_conversation: bool,
) -> None:
    if new_conversation:
        context = replace(context, conversation=None)
    agent = DiscoveringAgent(release=asyncio.Event())
    first = asyncio.create_task(manager.discover(agent, context, refresh=True))
    await agent.entered.wait()
    second = asyncio.create_task(manager.discover(agent, context, refresh=True))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert agent.release is not None
    agent.release.set()
    assert (await second).status == "ready"
    assert len(agent.calls) == 1


@pytest.mark.parametrize("new_conversation", [False, True])
async def test_invalidation_discards_late_result(
    manager: CommandManager, context: CommandContext, new_conversation: bool
) -> None:
    if new_conversation:
        context = replace(context, conversation=None)
    agent = DiscoveringAgent(release=asyncio.Event())
    waiting = asyncio.create_task(manager.discover(agent, context))
    await agent.entered.wait()
    manager.invalidate(agent_id=agent.id)
    assert agent.release is not None
    agent.release.set()
    assert (await waiting).status == "loading"
    assert (await manager.discover(agent, context)).status == "ready"
    assert len(agent.calls) == 2


async def test_invalidation_filters_preserve_other_catalogs(
    manager: CommandManager,
    context: CommandContext,
) -> None:
    agent = DiscoveringAgent()
    conversation = Conversation(thread_id=uuid7(), agent_tentacle_id=agent.id)
    existing = replace(context, conversation=conversation)
    await manager.discover(agent, context)
    await manager.discover(agent, existing)
    manager.invalidate(agent_id="other", conversation_id=conversation.id)
    await manager.discover(agent, existing)
    assert len(agent.calls) == 2
    manager.invalidate(agent_id=agent.id, conversation_id=conversation.id)
    await manager.discover(agent, context)
    await manager.discover(agent, existing)
    assert len(agent.calls) == 3


@pytest.mark.parametrize("failure", ["exception", "wrong-owner", "timeout"])
async def test_probe_failures_are_explicit_and_retryable(
    manager: CommandManager,
    context: CommandContext,
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = DiscoveringAgent(
        failure=failure == "exception",
        owner="other" if failure == "wrong-owner" else None,
        release=asyncio.Event() if failure == "timeout" else None,
    )
    monkeypatch.setattr(CommandManager, "timeout", 0.01)
    result = await manager.discover(agent, context)
    assert result.message is not None
    assert result.status == "failed"
    assert "private" not in result.message
    agent.failure = False
    agent.owner = None
    agent.release = None
    assert (await manager.discover(agent, context)).status == "ready"


async def test_cache_evicts_least_recently_used_catalog(
    context: CommandContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(CommandManager, "capacity", 2)
    manager = Octomate().commands
    agent = DiscoveringAgent()
    other = replace(context, user_id=uuid7())
    await manager.discover(agent, context)
    await manager.discover(agent, other)
    await manager.discover(agent, context)
    await manager.discover(agent, replace(context, user_id=uuid7()))
    assert len(manager.catalogs) == 2
    await manager.discover(agent, context)
    assert len(agent.calls) == 3
    await manager.discover(agent, other)
    assert len(agent.calls) == 4


async def test_shutdown_includes_invalidated_probes(
    manager: CommandManager,
    context: CommandContext,
) -> None:
    agent = DiscoveringAgent(release=asyncio.Event())
    waiting = asyncio.create_task(manager.discover(agent, context))
    await agent.entered.wait()
    manager.invalidate()
    agent.entered.clear()
    replacement = asyncio.create_task(manager.discover(agent, context))
    await agent.entered.wait()
    assert len(agent.calls) == 2
    assert len(manager.probes) == 1
    assert (await waiting).status == "loading"
    await manager.close()
    with pytest.raises(asyncio.CancelledError):
        await replacement
    assert not manager.probes
    assert not manager.catalogs


async def test_wrong_conversation_agent_is_rejected_before_discovery(
    manager: CommandManager,
    context: CommandContext,
) -> None:
    agent = DiscoveringAgent()
    context = replace(
        context, conversation=Conversation(thread_id=uuid7(), agent_tentacle_id="other")
    )
    with pytest.raises(ValueError, match="another agent"):
        await manager.discover(agent, context)
    assert agent.calls == []


async def test_runtime_cancellation_does_not_poison_cache(
    manager: CommandManager,
    context: CommandContext,
) -> None:
    agent = DiscoveringAgent(release=asyncio.Event())
    waiting = asyncio.create_task(manager.discover(agent, context))
    await agent.entered.wait()
    next(iter(manager.probes.values())).cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    agent.release = None
    assert (await manager.discover(agent, context)).status == "ready"


async def test_host_shutdown_drains_command_discovery(context: CommandContext) -> None:
    app = Octomate()
    agent = DiscoveringAgent(octomate=app, release=asyncio.Event())
    app.connect(agent)
    async with app.run_tentacles():
        waiting = asyncio.create_task(app.commands.discover(agent, context))
        await agent.entered.wait()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not app.commands.probes
    assert not app.commands.catalogs
    assert agent.routes == []


async def test_new_composer_catalog_is_reused_until_conversation_exists(
    manager: CommandManager,
    context: CommandContext,
) -> None:
    agent = DiscoveringAgent()
    composer = replace(context, conversation=None)
    await manager.discover(agent, composer)
    await manager.discover(agent, composer)
    assert len(agent.calls) == 1
    assert list(manager.catalogs) == [
        CommandCatalogKey(context.user_id, agent.id, None)
    ]
    assert not manager.probes
    await manager.discover(agent, context)
    assert len(agent.calls) == 2
    assert len(manager.catalogs) == 2


@pytest.mark.parametrize("scope", ["user", "agent", "workspace"])
async def test_new_composer_catalog_scope(
    manager: CommandManager, context: CommandContext, scope: str
) -> None:
    agent = DiscoveringAgent()
    context = replace(context, conversation=None)
    await manager.discover(agent, context)
    changed_agent = agent
    if scope == "user":
        changed = replace(context, user_id=uuid7())
    elif scope == "agent":
        changed_agent = DiscoveringAgent(id="other")
        changed = replace(context, agent_id=changed_agent.id)
    else:
        assert context.cwd is not None
        changed = replace(context, cwd=context.cwd / "other")
    result = await manager.discover(changed_agent, changed)
    assert result.context.matches(changed)
    assert len(manager.catalogs) == (1 if scope == "workspace" else 2)
    await manager.discover(agent, context)
    assert len(agent.calls) == {"user": 2, "agent": 1, "workspace": 3}[scope]
    if scope == "agent":
        assert len(changed_agent.calls) == 1


async def test_loading_catalog_is_replaced_when_discovery_completes(
    manager: CommandManager,
    context: CommandContext,
) -> None:
    agent = DiscoveringAgent(release=asyncio.Event())
    waiting = asyncio.create_task(manager.discover(agent, context))
    await agent.entered.wait()
    assert next(iter(manager.catalogs.values())).status == "loading"
    assert agent.release is not None
    agent.release.set()
    assert (await waiting).status == "ready"
    assert next(iter(manager.catalogs.values())).status == "ready"
    await manager.discover(agent, context)
    assert len(agent.calls) == 1


async def test_changed_metadata_discards_old_probe_and_replaces_same_key(
    manager: CommandManager,
    context: CommandContext,
) -> None:
    agent = DiscoveringAgent(release=asyncio.Event())
    old = asyncio.create_task(manager.discover(agent, context))
    await agent.entered.wait()
    assert context.cwd is not None
    changed = replace(context, cwd=context.cwd / "new-workspace")
    agent.entered.clear()
    new = asyncio.create_task(manager.discover(agent, changed))
    await agent.entered.wait()
    assert agent.release is not None
    agent.release.set()
    assert (await old).status == "loading"
    assert (await new).status == "ready"
    assert len(manager.catalogs) == 1
    assert not manager.probes
    await manager.discover(agent, changed)
    assert len(agent.calls) == 2


async def test_replacement_drains_old_probe_before_concurrent_callers_share_it(
    manager: CommandManager,
    context: CommandContext,
) -> None:
    cleanup_started = asyncio.Event()
    finish_cleanup = asyncio.Event()

    class CleaningAgent(DiscoveringAgent):
        async def probe_commands(self, context: CommandContext) -> CommandCatalog:
            try:
                return await super().probe_commands(context)
            finally:
                if len(self.calls) == 1:
                    cleanup_started.set()
                    await finish_cleanup.wait()

    agent = CleaningAgent(release=asyncio.Event())
    old = asyncio.create_task(manager.discover(agent, context))
    await agent.entered.wait()
    agent.entered.clear()
    assert context.cwd is not None
    changed = replace(context, cwd=context.cwd / "new-workspace")
    first = asyncio.create_task(manager.discover(agent, changed))
    await cleanup_started.wait()
    second = asyncio.create_task(manager.discover(agent, changed))
    await asyncio.sleep(0)
    assert len(agent.calls) == 1
    assert len(manager.probes) == 1
    finish_cleanup.set()
    await agent.entered.wait()
    assert len(agent.calls) == 2
    assert len(manager.probes) == 1
    assert context.conversation is not None
    assert list(manager.probes) == [
        CommandCatalogKey(context.user_id, context.agent_id, context.conversation.id)
    ]
    assert agent.release is not None
    agent.release.set()
    assert (await old).status == "loading"
    assert (await first).status == "ready"
    assert (await second).status == "ready"
    assert not manager.probes


async def test_context_metadata_snapshots_mutable_native_session(
    manager: CommandManager,
    context: CommandContext,
) -> None:
    agent = DiscoveringAgent()
    await manager.discover(agent, context)
    assert context.conversation is not None
    context.conversation.external_id = "new-session"
    await manager.discover(agent, context)
    assert len(agent.calls) == 2
    assert len(manager.catalogs) == 1
    assert agent.calls[0].conversation is not None
    assert agent.calls[0].conversation.external_id is None
