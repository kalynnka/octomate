from __future__ import annotations

from typing import Literal

# The approval level a conversation grants an external coding agent (Claude):
# prompt every gated tool ("default"), auto-accept edits, or skip all gating
# ("bypass_permissions"). Our values are snake_case; the Claude tentacle maps
# them to the SDK's camelCase `permission_mode` at the boundary.
#
# Here rather than on the schema because a project declares the mode its
# conversations start under, and a project cannot import the conversation that
# imports it.
ConversationPermissionMode = Literal["default", "accept_edits", "bypass_permissions"]

# Which surface a message is on: a direct message, a whole group chat, or a flat
# thread — a Slack agent thread, a Lark sub-thread in a group chat. `thread_id` names
# the thread; it is set exactly when the type is "thread".
ChatType = Literal["dm", "group", "thread"]
