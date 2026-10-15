"""Owned artifact loading, including access through a thread's visible ledger."""

from arcanus.materia.sqlalchemy import noload
from pydantic import UUID7, AnyHttpUrl

from octomate.config.artifacts import ArtifactConfig
from octomate.database import async_session
from octomate.managers.base import Manager
from octomate.managers.mcp import McpManager, McpUnavailable
from octomate.managers.thread import ThreadManager, in_ledger
from octomate.schemas.artifacts import McpAppArtifact, McpAppView
from octomate.schemas.thread import ThreadArtifact
from octomate.schemas.user import User


class ArtifactUnavailable(LookupError):
    """The artifact, its ledger reference, or its installation is inaccessible."""


class ArtifactSandboxUnavailable(ValueError):
    """Artifact loading requires a configured sandbox on a separate origin."""


class ArtifactManager(Manager):
    """Authorize artifact ownership and hosting before fetching saved UI resources."""

    def __init__(
        self, *, config: ArtifactConfig, mcp: McpManager, threads: ThreadManager
    ) -> None:
        self.config: ArtifactConfig = config
        self.mcp: McpManager = mcp
        self.threads: ThreadManager = threads

    async def load(
        self, artifact_id: UUID7, user: User, *, console_origin: AnyHttpUrl
    ) -> McpAppView:
        """Read owned content independently of its thread, without executing tools.

        The saved installation must still be owned, enabled, and authorized. A
        separate sandbox origin is required before any UI resource is fetched.
        """
        async with async_session() as session:
            artifact = await session.one_or_none(
                McpAppArtifact,
                expressions=[
                    McpAppArtifact["id"] == artifact_id,
                    McpAppArtifact["owner_id"] == user.id,
                ],
            )
        if artifact is None:
            raise ArtifactUnavailable("Artifact unavailable")
        sandbox = self.config.sandbox_origin
        if sandbox is None or (sandbox.scheme, sandbox.host, sandbox.port) == (
            console_origin.scheme,
            console_origin.host,
            console_origin.port,
        ):
            raise ArtifactSandboxUnavailable(
                "Configure a separate artifact sandbox origin"
            )
        try:
            resource = await self.mcp.app_resource(user, artifact.invocation)
        except McpUnavailable as error:
            raise ArtifactUnavailable("Artifact installation unavailable") from error
        return McpAppView(artifact=artifact, resource=resource, sandbox_origin=sandbox)

    async def load_from_message(
        self,
        thread_id: UUID7,
        message_id: UUID7,
        user: User,
        *,
        console_origin: AnyHttpUrl,
    ) -> McpAppView:
        """Read an owned artifact through an accessible thread and visible reference.

        Inherited references obey the fork's cutoff. Thread participation does not
        grant content ownership; the standalone load applies that check as well.
        """
        thread = await self.threads.get(thread_id, with_messages=False, user_id=user.id)
        if thread is None:
            raise ArtifactUnavailable("Thread unavailable")
        async with async_session() as session:
            message = await session.one_or_none(
                ThreadArtifact,
                expressions=[ThreadArtifact["id"] == message_id, in_ledger(thread.id)],
                options=[noload("*")],
            )
        if message is None:
            raise ArtifactUnavailable("Artifact reference unavailable")
        return await self.load(message.artifact_id, user, console_origin=console_origin)
