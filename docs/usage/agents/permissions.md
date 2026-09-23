# Permissions and approvals

## Permission modes

Each runtime has its own approval posture, handed over on its own terms:

| Agent | Modes | Default |
|---|---|---|
| Claude Code | `default`, `acceptEdits`, `plan`, `bypassPermissions`, `dontAsk`, `auto`, the SDK's own scale | `default` |
| Codex | `user_review` (ask), `auto_review` (approve for me), `full_access` | `user_review` |
| DeepSeek Harness | Whatever presets the harness reports, `workspace-write` and `danger-full-access` out of the box | `workspace-write` |
| Inkling | `default`, `dontAsk`, `bypassPermissions` | `default` |

The block's `permission_mode` is the fallback for a conversation that carries no
posture of its own. Trunkline can switch a conversation's posture, and the
conversation keeps it from then on. Octomate adds no sandbox of its own: what a run
may touch inside its [workspace](../workspaces.md) is the runtime's rule.

## Approvals and questions

When a tool needs permission, or the agent asks the user something, the run raises
an **action** and the channel shows a card. Every runtime reaches the same cards
through its own mechanism, and the answer flows back the same way. A card that
nobody answers expires after `approval_timeout`, one hour by default, and the pending
tool is denied so the run can finish. "Allow for session" grants are persisted on
the conversation and honoured on later turns. [Approvals and questions](../actions.md)
has the details, including which runtimes survive a restart mid-question.
