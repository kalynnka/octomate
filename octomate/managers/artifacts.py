"""Owned artifact loading, including access through a thread's visible ledger."""

from urllib.parse import urlencode

from arcanus.materia.sqlalchemy import noload
from pydantic import UUID7, AnyHttpUrl

from octomate.config.artifacts import ArtifactConfig
from octomate.database import async_session
from octomate.managers.base import Manager
from octomate.managers.files import FileManager
from octomate.managers.mcp import McpManager, McpUnavailable
from octomate.managers.thread import ThreadManager, in_ledger
from octomate.schemas.artifacts import (
    Artifact,
    ArtifactOverview,
    ArtifactSandboxPolicy,
    HtmlArtifact,
    HtmlView,
    McpAppArtifact,
    McpAppView,
)
from octomate.schemas.mcp import McpAppResourceCSP
from octomate.schemas.thread import ThreadArtifact
from octomate.schemas.user import User


class ArtifactUnavailable(LookupError):
    """The artifact, its ledger reference, or its installation is inaccessible."""


class ArtifactSandboxUnavailable(ValueError):
    """Artifact loading requires a configured sandbox on a separate origin."""


class ArtifactViewerUnavailable(ValueError):
    """An absolute artifact link requires a configured viewer origin."""


class ArtifactManager(Manager):
    """Authorize artifact ownership and hosting before fetching saved UI resources."""

    def __init__(
        self,
        *,
        config: ArtifactConfig,
        mcp: McpManager,
        threads: ThreadManager,
        files: FileManager,
    ) -> None:
        self.config: ArtifactConfig = config
        self.mcp: McpManager = mcp
        self.threads: ThreadManager = threads
        self.files: FileManager = files

    async def overview(self, artifact_id: UUID7, user: User) -> ArtifactOverview:
        """Read an owned artifact's card metadata without contacting its provider.

        Only the saved title, provider name, identity, and viewer URL leave this
        method. An overview does not grant access to its recipients or guarantee
        that the installation is still available; the viewer checks that on open.
        Missing viewer configuration raises ArtifactViewerUnavailable.
        """
        async with async_session() as session:
            artifact = await session.one_or_none(
                Artifact,
                expressions=[
                    Artifact["id"] == artifact_id,
                    Artifact["owner_id"] == user.id,
                ],
            )
        if not isinstance(artifact, McpAppArtifact | HtmlArtifact):
            raise ArtifactUnavailable("Artifact unavailable")
        origin = self.config.viewer_origin
        if origin is None:
            raise ArtifactViewerUnavailable("Configure artifacts.viewer_origin")
        return ArtifactOverview(
            artifact_id=artifact.id,
            title=artifact.title,
            provider_name=artifact.invocation.provider_name
            if isinstance(artifact, McpAppArtifact)
            else "Generated HTML",
            viewer_url=AnyHttpUrl(
                f"{str(origin).rstrip('/')}{self.config.path}/{artifact.id}"
            ),
        )

    async def load(
        self, artifact_id: UUID7, user: User, *, console_origin: AnyHttpUrl
    ) -> McpAppView | HtmlView:
        """Read owned content independently of its thread, without executing tools.

        HTML is read from the owner's stored file. An MCP installation must still
        be owned, enabled, and authorized. Both require a separate sandbox origin.
        """
        async with async_session() as session:
            artifact = await session.one_or_none(
                Artifact,
                expressions=[
                    Artifact["id"] == artifact_id,
                    Artifact["owner_id"] == user.id,
                ],
            )
        if not isinstance(artifact, McpAppArtifact | HtmlArtifact):
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
        resource = None
        if isinstance(artifact, McpAppArtifact):
            try:
                resource = await self.mcp.app_resource(user, artifact.invocation)
            except McpUnavailable as error:
                raise ArtifactUnavailable(
                    "Artifact installation unavailable"
                ) from error
        policy = ArtifactSandboxPolicy(
            parent_origin=console_origin,
            content_kind=artifact.kind,
            csp=(resource.ui.csp if resource else None) or McpAppResourceCSP(),
        )
        query = urlencode({"policy": policy.model_dump_json(by_alias=True)})
        if isinstance(artifact, HtmlArtifact):
            try:
                data = await self.files.read(artifact.file_id, owner_id=user.id)
            except FileNotFoundError as error:
                raise ArtifactUnavailable("Artifact file unavailable") from error
            return HtmlView(
                artifact=artifact,
                html=data.decode("utf-8"),
                sandbox_origin=sandbox,
                sandbox_url=AnyHttpUrl(f"{sandbox}artifacts/sandbox?{query}"),
                viewer_path=f"{self.config.path}/{artifact.id}",
            )
        assert resource is not None
        return McpAppView(
            artifact=artifact,
            resource=resource,
            sandbox_origin=sandbox,
            sandbox_url=AnyHttpUrl(f"{sandbox}artifacts/sandbox?{query}"),
            viewer_path=f"{self.config.path}/{artifact.id}",
        )

    async def load_from_message(
        self,
        thread_id: UUID7,
        message_id: UUID7,
        user: User,
        *,
        console_origin: AnyHttpUrl,
    ) -> McpAppView | HtmlView:
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
