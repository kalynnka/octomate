"""Command discovery and guarded execution for host-authorized contexts."""

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import ClassVar, NamedTuple

from cachetools import LRUCache
from pydantic_ai import AgentCapability

from octomate.capabilities.harness.deferred import DeferredSuspender
from octomate.capabilities.harness.react import ReactEventStream, ReactStreamEvent
from octomate.managers.base import Manager
from octomate.managers.gateway import OctomateSession
from octomate.schemas.commands import (
    CommandCatalog,
    CommandContext,
    CommandError,
    CommandInvocation,
    CommandOutcome,
)
from octomate.tentacles.agent import AgentTentacle

logger = logging.getLogger(__name__)


class CommandCatalogKey(NamedTuple):
    """One user's agent catalog, before or after a conversation exists."""

    user_id: uuid.UUID
    agent_id: str
    conversation_id: uuid.UUID | None


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

    def __init__(self) -> None:
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
    async def execute[OutputT, DepsT](
        self,
        agent: AgentTentacle[OutputT, DepsT],
        context: CommandContext,
        invocation: CommandInvocation,
        *,
        session: OctomateSession | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        capabilities: list[AgentCapability[DepsT]] | None = None,
    ) -> AsyncGenerator[
        CommandOutcome | AsyncGenerator[ReactStreamEvent[OutputT], None]
    ]:
        """Validate and execute under the conversation's active-turn guard.

        The caller supplies freshly resolved, authorized context and deduplicates
        deliveries before entering. Execution requires an existing conversation;
        it never silently creates one. A fresh runtime catalog determines command
        membership and attachment support. Arguments pass through unchanged.

        Consume streamed events inside this context: it owns their cleanup and
        holds the guard until they close. Stream failures propagate to the caller's
        transport; direct adapter failures become failed outcomes. Every attempted
        invocation invalidates the conversation's catalogs when this context exits.
        """
        conversation = context.conversation
        if conversation is None:
            yield CommandError(
                status="unavailable",
                message="Start a conversation before running a command.",
            )
            return
        if session is not None and (
            session.conversation_id != conversation.id
            or session.current_agent_id != agent.id
        ):
            raise ValueError("gateway session belongs to another command context")
        gateway = agent.octomate.gateway
        if conversation.id in gateway.sessions:
            yield CommandError(
                status="busy", message="This conversation already has an active turn."
            )
            return
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(
                gateway.driving(session, conversation_id=conversation.id)
            )
            catalog = await self.discover(agent, context, refresh=True)
            if catalog.status != "ready":
                yield CommandError(
                    status="stale" if catalog.status == "loading" else catalog.status,
                    message=catalog.message
                    or "The command catalog changed; discover it again.",
                )
                return
            descriptors = {
                descriptor.id: descriptor for descriptor in catalog.descriptors
            }
            descriptor = descriptors.get(invocation.command_id)
            if descriptor is None:
                yield CommandError(
                    status="unknown", message="This command is no longer available."
                )
                return
            if invocation.attachments and descriptor.accepts_attachments is not True:
                yield CommandError(
                    status="unsupported",
                    message="This command does not declare attachment support.",
                )
                return
            stack.callback(self.invalidate, conversation_id=conversation.id)
            try:
                result = await agent.execute_command(
                    context,
                    invocation,
                    deferred_suspender=deferred_suspender,
                    capabilities=capabilities,
                )
            except Exception:
                logger.exception("Command execution failed for agent %s", agent.id)
                result = CommandError(
                    status="failed",
                    message="Command execution failed; its effects may already have occurred.",
                )
            if isinstance(result, ReactEventStream):
                yield await stack.enter_async_context(result)
            else:
                yield result

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
