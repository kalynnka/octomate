"""Stub `Ink` for the DevUI tentacle.

The DevUI doesn't talk to a real platform — outbound happens inside the SSE
response stream, not via Ink. We satisfy the protocol with no-op methods so
`ChannelTentacle.__init__` can call `self.ink.inspect()` without exploding.
"""

from typing import Any

from octomate.schemas.segments import MessageSegment
from octomate.schemas.session import UserProfile

DEV_UI_PROFILE = UserProfile(user_id="inkling", name="Inkling")


class StubInk:
    def inspect(self) -> UserProfile:
        return DEV_UI_PROFILE

    async def get_user_profile(self, user_id: str) -> UserProfile:
        return UserProfile(user_id=user_id, name=user_id)

    async def upload_media(self, data: bytes) -> str | None:
        return None

    async def download_media(
        self, resource_id: str, **kwargs: Any
    ) -> tuple[bytes, str] | None:
        return None

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        segments: list[MessageSegment],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str | None:
        # Outbound for the DevUI happens via the SSE stream in GraphAdapter,
        # not via the Ink-typed twitch path. This method is unreachable in
        # normal operation; if something does call it, we drop silently.
        return None
