"""Native Codex HTML capture layered onto the shared file stream.

Only root assistant visualization references authorize reads. The snapshots come
from the session workspace; raw rollout lines and byte offsets stay unchanged.
"""

import asyncio
from pathlib import Path

from octomate_protocol.stream import StreamLine
from octomate_protocol.visualizations import HtmlSnapshot, codex_html_references
from pydantic import UUID7

from octomate_cli.streaming.files import SessionTail


class CodexSessionTail(SessionTail):
    """Attach bounded HTML snapshots to the root Codex rollout's frames."""

    async def frame(
        self, conversation_id: UUID7, start: int, end: int, raw: bytes
    ) -> StreamLine:
        frame = await super().frame(conversation_id, start, end, raw)
        if conversation_id != self.conversation_id or not self.cwd:
            return frame
        workspace = Path(self.cwd)
        frame.visualizations = [
            HtmlSnapshot(
                reference=reference,
                html=await asyncio.to_thread(reference.read, workspace),
            )
            for reference in codex_html_references(frame.line)
        ]
        return frame
