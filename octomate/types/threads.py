from __future__ import annotations

from typing import Literal

# Here rather than on either side because the ORM model and the schema both spell these
# out, and a divergence between the two is only found at runtime.

ThreadStatus = Literal["active", "closed"]

# A thread's surface: its `ChatType`, plus the native clients, whose threads are
# ingested rather than driven by a channel.
ThreadKind = Literal["dm", "group", "thread", "native_thread"]

# Synthetic ids a native client's thread and conversation are filed under; no channel
# and no agent is registered as either. Here rather than in `schemas.thread` — which
# still re-exports them — so `types.permissions` can name them without importing a
# schema that imports it back.
CLAUDE_NATIVE_ID = "claude-native"
CODEX_NATIVE_ID = "codex-native"
DEEPSEEK_NATIVE_ID = "deepseek-native"
NATIVE_TENTACLE_IDS: frozenset[str] = frozenset(
    {CLAUDE_NATIVE_ID, CODEX_NATIVE_ID, DEEPSEEK_NATIVE_ID}
)
# The user_id a native handoff's *source* address carries — runtime attribution
# for "where this came from". Never a person: a native session's ledger rows are
# attributed to the verified bearer's own profile (`channel_user_id` = username).
NATIVE_CHANNEL_USER_ID = "native"

ThreadMessageDirection = Literal["inbound", "outbound"]
ChannelActorKind = Literal["human", "agent", "bot", "system"]
MessageBindingKind = Literal[
    "request_source",
    "assistant_reply",
    "assistant_send",
]
