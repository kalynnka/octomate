from __future__ import annotations

from typing import Literal

# Here rather than on either side because the ORM model and the schema both spell these
# out, and a divergence between the two is only found at runtime.

ThreadStatus = Literal["active", "closed"]

# A thread's surface: its `ChatType`, plus the native clients, whose threads are
# ingested rather than driven by a channel.
ThreadKind = Literal["dm", "group", "thread", "native_thread"]

ThreadMessageDirection = Literal["inbound", "outbound"]
ChannelActorKind = Literal["human", "agent", "bot", "system"]
MessageBindingKind = Literal[
    "request_source",
    "assistant_reply",
    "assistant_send",
]
