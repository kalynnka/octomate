"""Command discovery and guarded execution for host-authorized contexts."""

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import replace
from typing import ClassVar, NamedTuple

from cachetools import LRUCache
from pydantic_ai import AgentCapability

from octomate.capabilities.harness.deferred import DeferredSuspender
from octomate.capabilities.harness.react import ReactStreamEvent
from octomate.config.channels import TrunklineChannelConfig
from octomate.managers.base import Manager
from octomate.managers.conversation import ConversationManager
from octomate.managers.gateway import GatewayManager, OctomateSession
from octomate.managers.thread import ThreadManager
from octomate.managers.user import UserManager
from octomate.managers.workspaces import WorkspaceManager
from octomate.schemas.commands import (
    CommandCatalog,
    CommandContext,
    CommandDescriptor,
    CommandError,
    CommandInvocation,
    CommandOutcome,
    CommandResult,
)
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import Thread, ThreadCommand, ThreadKey
from octomate.schemas.user import UserProfile
from octomate.tentacles.agent import AgentTentacle
from octomate.tentacles.base import Tentacle
from octomate.tentacles.channel import ChannelTentacle

logger = logging.getLogger(__name__)


class CommandCatalogKey(NamedTuple):
    """One user's agent catalog, before or after a conversation exists."""

    user_id: uuid.UUID
    agent_id: str
    conversation_id: uuid.UUID | None


class ValidatedCommand(NamedTuple):
    """Authorized command inputs, valid while the validation scope remains open."""

    surface: Thread | None
    profile: UserProfile | None
    descriptor: CommandDescriptor


class CommandManager(Manager):
    """Keep one LRU catalog per user, agent and conversation until invalidated.

    Callers must resolve and authorize a fresh context before every lookup, even
    a cache hit. Adapters must call `invalidate` on runtime reconnects and
    catalog-change events. Context changes replace the existing catalog and
    discard its pending result. Discovery errors are explicit, never empty lists.
    """

    capacity: ClassVar[int] = 256
    timeout: ClassVar[float] = 10.0

    catalogs: LRUCache[CommandCatalogKey, CommandCatalog]
    probes: dict[CommandCatalogKey, asyncio.Task[CommandCatalog]]
    tentacles: dict[str, Tentacle]
    users: UserManager
    conversations: ConversationManager
    threads: ThreadManager
    workspaces: WorkspaceManager
    gateway: GatewayManager

    def __init__(
        self,
        *,
        tentacles: dict[str, Tentacle],
        users: UserManager,
        conversations: ConversationManager,
        threads: ThreadManager,
        workspaces: WorkspaceManager,
        gateway: GatewayManager,
    ) -> None:
        self.tentacles = tentacles
        self.users = users
        self.conversations = conversations
        self.threads = threads
        self.workspaces = workspaces
        self.gateway = gateway
        self.catalogs = LRUCache(maxsize=self.capacity)
        self.probes = {}

    async def discover(
        self,
        agent: AgentTentacle,
        context: CommandContext,
        *,
        refresh: bool = False,
    ) -> CommandCatalog:
        """Read or refresh a catalog, sharing active discovery in the same context.

        Loading and failed results are reprobed on the next lookup. Canceling one
        caller leaves shared discovery alive under its timeout. New composers
        share a catalog per user and agent while their contexts match.
        """
        conversation = context.conversation
        if context.agent_id != agent.id or (
            conversation is not None and conversation.agent_tentacle_id != agent.id
        ):
            raise ValueError("command context belongs to another agent")

        key = CommandCatalogKey(
            user_id=context.user_id,
            agent_id=context.agent_id,
            conversation_id=conversation.id if conversation is not None else None,
        )
        while True:
            catalog = self.catalogs.get(key)
            task = self.probes.get(key)
            if catalog is not None and catalog.context.matches(context):
                if task is not None and not task.done():
                    return (await asyncio.shield(task)).snapshot()
                if not refresh and catalog.status not in {"loading", "failed"}:
                    return catalog.snapshot()
            if task is None or task.done():
                break
            self.catalogs.pop(key, None)
            if not task.cancelling():
                task.cancel()
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
        self.catalogs.pop(key, None)
        catalog = CommandCatalog(context=context, status="loading").snapshot()
        task = asyncio.create_task(self.probe(agent, catalog))
        self.probes[key] = task
        self.catalogs[key] = catalog

        def complete(task: asyncio.Task[CommandCatalog]) -> None:
            if self.probes.get(key) is task:
                del self.probes[key]

        task.add_done_callback(complete)
        return (await asyncio.shield(task)).snapshot()

    async def probe(
        self, agent: AgentTentacle, catalog: CommandCatalog
    ) -> CommandCatalog:
        """Replace a loading catalog only while it still represents this discovery."""
        context = catalog.context
        key = CommandCatalogKey(
            user_id=context.user_id,
            agent_id=context.agent_id,
            conversation_id=context.conversation.id
            if context.conversation is not None
            else None,
        )
        try:
            async with asyncio.timeout(self.timeout):
                result = await agent.probe_commands(context)
                if not result.context.matches(context):
                    raise ValueError("command catalog belongs to another context")
                result = result.snapshot()
                descriptors = {
                    entry
                    for entry in result.descriptors
                    if entry.name.casefold() != "goal"
                }
                if descriptors != result.descriptors:
                    result.descriptors = descriptors
                    result.limitations.append(
                        "Goal commands are excluded until automatic continuation is supported."
                    )
        except asyncio.CancelledError:
            if self.catalogs.get(key) is not catalog:
                return CommandCatalog(context=context, status="loading")
            raise
        except Exception:
            logger.exception("Command discovery failed for agent %s", agent.id)
            result = CommandCatalog(
                context=context,
                status="failed",
                message="Command discovery failed; refresh to retry.",
            )
        if self.catalogs.get(key) is not catalog:
            return CommandCatalog(context=context, status="loading")
        self.catalogs[key] = result
        return result

    @asynccontextmanager
    async def validate(
        self,
        agent: AgentTentacle,
        context: CommandContext,
        invocation: CommandInvocation,
        *,
        delivery_id: str,
        session: OctomateSession | None = None,
    ) -> AsyncGenerator[ValidatedCommand | CommandOutcome]:
        """Hold the turn guard while validating a delivery and its execution.

        Refresh the catalog, then check access and current context once. Refusals
        and matching retries yield their outcome. Otherwise yield a ValidatedCommand
        with the authorized surface, profile and descriptor. The surface and
        profile may be absent before a conversation exists. Prepare available user
        capabilities and call `execute` inside this scope; validation never dispatches
        a command. The guard remains held through the caller's runtime cleanup.
        """
        if not delivery_id:
            raise ValueError("command deliveries require a delivery_id")
        conversation_id = context.conversation.id if context.conversation else None
        if session is not None and (
            session.conversation_id != conversation_id
            or session.current_agent_id != agent.id
        ):
            raise ValueError("gateway session belongs to another command context")
        if conversation_id is not None and conversation_id in self.gateway.sessions:
            yield CommandError(
                status="busy", message="This conversation already has an active turn."
            )
            return
        async with self.gateway.driving(session, conversation_id=conversation_id):
            catalog = await self.discover(agent, context, refresh=True)
            channel = self.tentacles.get(context.address.channel_tentacle_id)
            if (
                not isinstance(channel, ChannelTentacle)
                or self.tentacles.get(context.agent_id) is not agent
                or agent.id not in channel.agent_ids
            ):
                yield CommandError(
                    status="unavailable",
                    message="The command agent is unavailable on this channel.",
                )
                return
            profile = await self.users.profile(channel.id, context.address.user_id)
            new_composer = (
                conversation_id is None
                and isinstance(channel.config, TrunklineChannelConfig)
                and context.address.user_id
                == context.address.chat_id
                == str(context.user_id)
                and context.address.channel_thread_id is None
            )
            if (profile is None and not new_composer) or (
                profile is not None and profile.user_id != context.user_id
            ):
                yield CommandError(
                    status="unavailable", message="The command surface is unavailable."
                )
                return
            surface = None
            current = context
            route_matches = True
            if conversation_id is not None:
                surface = await self.threads.get(
                    ThreadKey.from_address(context.address),
                    with_messages=False,
                    user_id=context.user_id,
                )
                try:
                    conversation = await self.conversations.get(
                        conversation_id, with_history=False
                    )
                except ValueError:
                    conversation = None
                thread = surface
                if (
                    surface is not None
                    and conversation is not None
                    and conversation.thread_id != surface.id
                ):
                    thread = await self.threads.get(
                        conversation.thread_id, with_messages=False
                    )
                if (
                    conversation is None
                    or thread is None
                    or surface is None
                    or (thread.parent_thread_id or thread.id) != surface.id
                ):
                    yield CommandError(
                        status="unavailable",
                        message="The command conversation is unavailable.",
                    )
                    return
                route_matches = (
                    conversation.agent_tentacle_id == agent.id
                    and not conversation.subagent_id
                    and surface.active_agent_tentacle_id == agent.id
                )
                current = replace(
                    context,
                    conversation=conversation,
                    cwd=self.workspaces.open(thread.id, await thread.project).path,
                    model=surface.active_model,
                    permission_mode=conversation.permission_mode
                    or agent.default_permission_mode,
                )
            try:
                current = replace(current, model=agent.resolve_model(current.model))
                if current.permission_mode is not None:
                    agent.check_permission_mode(current.permission_mode)
            except ValueError:
                current = None
            if current is None or not route_matches or not context.matches(current):
                yield CommandError(
                    status="stale",
                    message="The command context changed; discover commands again.",
                )
                return
            recorded = (
                await self.threads.find_message(surface.id, delivery_id, "inbound")
                if surface is not None
                else None
            )
            if recorded is not None:
                if (
                    not isinstance(recorded, ThreadCommand)
                    or profile is None
                    or recorded.sender_id != profile.id
                    or recorded.conversation_id != conversation_id
                    or recorded.agent_tentacle_id != agent.id
                    or recorded.invocation != invocation
                ):
                    yield CommandError(
                        status="failed",
                        message="This delivery ID already identifies another request.",
                    )
                    return
                yield recorded.outcome or CommandError(
                    status="failed",
                    message="This command delivery was already accepted, but no outcome "
                    "was recorded; its effects may already have occurred.",
                )
                return
            descriptors = {
                descriptor.id: descriptor for descriptor in catalog.descriptors
            }
            descriptor = descriptors.get(invocation.command_id)
            if catalog.status != "ready":
                yield CommandError(
                    status="stale" if catalog.status == "loading" else catalog.status,
                    message=catalog.message
                    or "The command catalog changed; discover it again.",
                )
            elif descriptor is None:
                yield CommandError(
                    status="unknown", message="This command is no longer available."
                )
            elif descriptor.unavailable_reason is not None:
                yield CommandError(
                    status="unavailable", message=descriptor.unavailable_reason
                )
            elif descriptor.requires_conversation and conversation_id is None:
                yield CommandError(
                    status="unavailable",
                    message="This command requires an existing conversation.",
                )
            elif invocation.attachments and descriptor.accepts_attachments is not True:
                yield CommandError(
                    status="unsupported",
                    message="This command does not declare attachment support.",
                )
            else:
                yield ValidatedCommand(surface, profile, descriptor)

    @asynccontextmanager
    async def execute[OutputT, DepsT](
        self,
        agent: AgentTentacle[OutputT, DepsT],
        context: CommandContext,
        invocation: CommandInvocation,
        validated: ValidatedCommand,
        *,
        delivery_id: str,
        deferred_suspender: DeferredSuspender | None = None,
        capabilities: list[AgentCapability[DepsT]] | None = None,
    ) -> AsyncGenerator[
        CommandOutcome | AsyncGenerator[ReactStreamEvent[OutputT], None]
    ]:
        """Record and execute a delivery inside its successful `validate` scope.

        Validation and user preparation belong to the caller. Persist the receipt
        before native dispatch, preserve arguments, and invalidate the catalog on
        exit. Consume streamed events inside this context so runtime cleanup and
        outcome recording finish before the validation scope releases its guard.
        Without a conversation, dispatch produces only a direct outcome: no
        receipt, run or thread is created, and retries are not deduplicated.
        Direct adapter failures become failed outcomes; stream errors and
        cancellation propagate to the caller.
        """
        conversation = context.conversation
        surface = validated.surface
        profile = validated.profile
        async with AsyncExitStack() as stack:
            receipt = None
            if conversation is not None:
                assert surface is not None
                assert profile is not None
                text = f"/{validated.descriptor.name}"
                if invocation.arguments:
                    text += f" {invocation.arguments}"
                intent = invocation.model_copy(deep=True)
                receipt = ThreadCommand(
                    thread_id=surface.id,
                    platform_message_id=delivery_id,
                    direction="inbound",
                    actor_kind="human",
                    user_id=context.address.user_id,
                    sender_id=profile.id,
                    agent_tentacle_id=agent.id,
                    conversation_id=conversation.id,
                    invocation=intent,
                    segments=[TextSegment(data={"text": text}), *intent.attachments],
                    message_text=text,
                )
                await self.threads.store_message(receipt, surface)
                stack.callback(self.invalidate, conversation_id=conversation.id)
            else:
                stack.callback(self.invalidate, agent_id=agent.id)
            interrupted = CommandError(
                status="failed",
                message="Command execution did not finish; its effects may already have occurred.",
            )
            try:
                stream = await stack.enter_async_context(
                    aclosing(
                        agent.execute_command(
                            context,
                            invocation,
                            deferred_suspender=deferred_suspender,
                            capabilities=capabilities,
                        )
                    )
                )
                result = await anext(stream)
                if isinstance(result, CommandResult | CommandError):
                    await stream.aclose()
                elif receipt is None:
                    await stream.aclose()
                    result = CommandError(
                        status="failed",
                        message="The command adapter emitted a run without a conversation.",
                    )
            except asyncio.CancelledError:
                if receipt is not None:
                    await self.threads.record_command_outcome(receipt.id, interrupted)
                raise
            except Exception:
                logger.exception("Command execution failed for agent %s", agent.id)
                result = CommandError(
                    status="failed",
                    message="Command execution failed; its effects may already have occurred.",
                )
            if isinstance(result, CommandResult | CommandError):
                if receipt is not None:
                    await self.threads.record_command_outcome(receipt.id, result)
                yield result
            else:
                assert receipt is not None
                yield await stack.enter_async_context(
                    self.record_stream(stream, result, receipt.id, interrupted)
                )

    @asynccontextmanager
    async def record_stream[OutputT](
        self,
        stream: AsyncGenerator[CommandOutcome | ReactStreamEvent[OutputT], None],
        first_event: ReactStreamEvent[OutputT],
        receipt_id: uuid.UUID,
        interrupted: CommandError,
    ) -> AsyncGenerator[AsyncGenerator[ReactStreamEvent[OutputT], None]]:
        """Record completion only after consumption and runtime cleanup both finish.

        Events remain owned by the normal stream consumer; a duplicate delivery
        receives completion status rather than replayed events. An abandoned or
        failed stream keeps the failure outcome prepared before dispatch.
        """
        completed = asyncio.Event()
        outcome: CommandOutcome = interrupted

        async def forward() -> AsyncGenerator[ReactStreamEvent[OutputT], None]:
            yield first_event
            async for event in stream:
                if isinstance(event, CommandResult | CommandError):
                    raise RuntimeError(
                        "An agent command stream yielded a direct outcome"
                    )
                yield event
            completed.set()

        try:
            async with aclosing(stream), aclosing(forward()) as forwarded:
                yield forwarded
            if completed.is_set():
                outcome = CommandResult()
        finally:
            await self.threads.record_command_outcome(receipt_id, outcome)

    def invalidate(
        self,
        *,
        agent_id: str | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> None:
        """Forget matching catalogs; pending results cannot repopulate them.

        No filters clears all. Invalidated waiters receive loading and must
        resolve context again before requesting another catalog.
        """
        for key in list(self.catalogs):
            if agent_id is not None and key.agent_id != agent_id:
                continue
            if conversation_id is not None and key.conversation_id != conversation_id:
                continue
            del self.catalogs[key]

    async def close(self) -> None:
        """Cancel and drain discovery before the host shuts down its agents."""
        probes = list(self.probes.values())
        for task in probes:
            task.cancel()
        await asyncio.gather(*probes, return_exceptions=True)
        self.catalogs.clear()
