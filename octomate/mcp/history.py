"""The history tools as MCP: the thread ledger of the person a session speaks
for, served beside the spells.

A family of its own on the one server, mounted under the `history` namespace.
Every return is text and bounded — a page of at most `HISTORY_PAGE_LIMIT`
messages, each clipped to `HISTORY_LINE_CHARS` — because these runtimes have none
of the spill bands that catch an oversized return for Inkling.
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from octomate.capabilities.history import HistoryCapability
from octomate.managers.gateway import OctomateSession
from octomate.managers.thread import ThreadManager
from octomate.mcp.base import capability_contract
from octomate.schemas.thread import ChannelActorKind, ThreadMessage
from octomate.schemas.user import UserProfile

# Characters of one message's line a page shows; the rest is a `before`/`after`
# call away, and a model can always search for the part it wants.
HISTORY_LINE_CHARS = 400
# Messages one call may return. Over it is refused, not clamped: the model asked
# for something it cannot have and should know.
HISTORY_PAGE_LIMIT = 50

# The served names, keyed by the capability's own tool names: one instruction
# template renders both, and the namespace the server mounts this family under
# supplies the `history_` half.
HISTORY_TOOL_NAMES: dict[str, str] = {
    "search_thread_history": "search",
    "read_thread_history_before": "read_before",
    "read_thread_history_after": "read_after",
}


def mount_history(
    mcp: FastMCP, octomate_session: OctomateSession, thread_manager: ThreadManager
) -> None:
    """Register the history tools on `mcp`, every call reading as the person the
    session `octomate_session` resolves to speaks for — a driven turn's user, or the
    registered person a native session's bearer named."""

    def reader(session: OctomateSession) -> UserProfile:
        if session.user_profile is None:
            raise ToolError(
                "This session speaks for nobody, and history is a person's: there "
                "is no one whose threads to read."
            )
        return session.user_profile

    async def anchor(session: OctomateSession, handle: str) -> ThreadMessage:
        try:
            return await thread_manager.chat_message(reader(session), handle)
        except ValueError as refusal:
            raise ToolError(str(refusal), log_level=logging.INFO) from refusal

    def page(limit: int) -> int:
        if limit > HISTORY_PAGE_LIMIT:
            raise ToolError(
                f"limit {limit} is over the page of {HISTORY_PAGE_LIMIT}; page in "
                "smaller steps."
            )
        return limit

    @mcp.tool(
        name=HISTORY_TOOL_NAMES["search_thread_history"],
        description=capability_contract(HistoryCapability.search_thread_history),
    )
    async def search_thread_history(
        query: str,
        actor_kind: ChannelActorKind | None = None,
        limit: int = 10,
        session: OctomateSession = octomate_session,
    ) -> str:
        rows = await thread_manager.search_chat_messages(
            reader(session), query, actor_kind=actor_kind, limit=page(limit)
        )
        return (
            "\n".join(
                line
                if len(line) <= HISTORY_LINE_CHARS
                else line[:HISTORY_LINE_CHARS] + "…"
                for line in map(str, rows)
            )
            or "(no messages)"
        )

    @mcp.tool(
        name=HISTORY_TOOL_NAMES["read_thread_history_before"],
        description=capability_contract(HistoryCapability.read_thread_history_before),
    )
    async def read_thread_history_before(
        message_id: str, limit: int = 10, session: OctomateSession = octomate_session
    ) -> str:
        found = await anchor(session, message_id)
        rows = await thread_manager.chat_messages_before(
            found.thread_id, found.id, limit=page(limit)
        )
        return (
            "\n".join(
                line
                if len(line) <= HISTORY_LINE_CHARS
                else line[:HISTORY_LINE_CHARS] + "…"
                for line in map(str, rows)
            )
            or "(no messages)"
        )

    @mcp.tool(
        name=HISTORY_TOOL_NAMES["read_thread_history_after"],
        description=capability_contract(HistoryCapability.read_thread_history_after),
    )
    async def read_thread_history_after(
        message_id: str, limit: int = 10, session: OctomateSession = octomate_session
    ) -> str:
        found = await anchor(session, message_id)
        rows = await thread_manager.chat_messages_after(
            found.thread_id, found.id, limit=page(limit)
        )
        return (
            "\n".join(
                line
                if len(line) <= HISTORY_LINE_CHARS
                else line[:HISTORY_LINE_CHARS] + "…"
                for line in map(str, rows)
            )
            or "(no messages)"
        )
