"""Artifact overviews rendered on the channel where they were created."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from octomate.capabilities.harness.events import ArtifactAvailableEvent
from octomate.schemas.artifacts import McpAppOverview
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import TextSegment
from octomate.tentacles.feelers.output import IMMessageID, SegmentsFeeler

if TYPE_CHECKING:
    from octomate.managers.artifacts import ArtifactManager
    from octomate.managers.user import UserManager


class ArtifactFeeler(ABC):
    """Resolve an owned overview and record its delivery separately from the reply.

    The original address is preserved, including its platform thread. Recipients
    see the saved title and provider; opening the viewer still requires ownership.
    Without a configured viewer origin, channel overviews are disabled.
    Subclasses implement only the platform presentation in ``send``.
    """

    def __init__(self, *, artifacts: ArtifactManager, users: UserManager) -> None:
        self.artifacts: ArtifactManager = artifacts
        self.users: UserManager = users

    @property
    def enabled(self) -> bool:
        """Whether this deployment can link to its artifact viewer."""
        return self.artifacts.config.viewer_origin is not None

    async def present(
        self, address: ChannelAddress, event: ArtifactAvailableEvent
    ) -> IMMessageID | None:
        # The artifact manager imports MCP, which loads channel classes at startup.
        from octomate.managers.artifacts import ArtifactUnavailable

        if not self.enabled:
            return None
        profile = await self.users.profile(address.channel_tentacle_id, address.user_id)
        if profile is None or profile.user_id is None:
            raise ArtifactUnavailable("Artifact owner unavailable")
        user = profile.user.value
        if user is None:
            raise ArtifactUnavailable("Artifact owner unavailable")
        overview = await self.artifacts.overview(event.artifact_id, user)
        message_id = await self.send(address, overview)
        await self.artifacts.threads.mark_presented(event.message_id, message_id)
        return message_id

    @abstractmethod
    async def send(
        self, address: ChannelAddress, overview: McpAppOverview
    ) -> IMMessageID | None:
        """Present already-authorized metadata without reading app content."""


class PlainTextArtifactFeeler(ArtifactFeeler):
    """A saved title, provider name, and literal viewer URL."""

    def __init__(
        self,
        *,
        artifacts: ArtifactManager,
        users: UserManager,
        segments: SegmentsFeeler,
    ) -> None:
        super().__init__(artifacts=artifacts, users=users)
        self.segments: SegmentsFeeler = segments

    async def send(
        self, address: ChannelAddress, overview: McpAppOverview
    ) -> IMMessageID | None:
        return await self.segments.present(
            address,
            [
                TextSegment(
                    data={
                        "text": f"{overview.title}\nMCP app · {overview.provider_name}"
                        f"\n{overview.viewer_url}"
                    }
                )
            ],
        )
