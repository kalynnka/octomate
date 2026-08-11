from __future__ import annotations

from typing import Literal

# Which surface a message is on: a direct message, a whole group chat, or a flat
# thread — a Slack agent thread, a Lark sub-thread in a group chat. `thread_id` names
# the thread; it is set exactly when the type is "thread".
ChatType = Literal["dm", "group", "thread"]
