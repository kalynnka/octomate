# Permission modes

Choose how much an agent may do without asking you. Start with its default mode
and change it when the task calls for a different level of control.

## Change a conversation's mode

Open the conversation in Trunkline and use its permission control to choose from
the modes that agent offers. The choice applies to that conversation's subsequent
work. To approve just one pending action, answer its card instead of changing the
whole conversation's mode.

For a native session, use the permission controls in your agent's own interface.
Octomate records that session; it does not change its local mode for you.

## Choose a mode { #permission-modes }

| Agent | Starting choice | Other choices to recognise |
|---|---|---|
| Claude Code | `default` | `plan` for planning, `acceptEdits` for automatically allowing edits, and other modes offered by Claude Code |
| Codex | `user_review` | `auto_review` delegates approval decisions; `full_access` removes the usual sandbox and approval prompts |
| DeepSeek Harness | `workspace-write` | The presets supplied by your harness, including `danger-full-access` |
| Inkling | `default` | `dontAsk` makes assumptions and denies tools needing approval; `bypassPermissions` allows those tools without asking |

A mode that skips questions does not necessarily grant more access. Inkling's
`dontAsk`, for example, can leave a task incomplete because a required action was
denied. Read the agent's report for assumptions and skipped work.

## Decide how much freedom the task needs

Keep review enabled when you want to check actions as they arise. An agent may
still use tools already allowed by its mode without showing a card. Choose a
broader mode only when you intend to grant that freedom for the conversation;
permission names do not mean exactly the same thing across agents.

Use **Allow for session**, when offered, to avoid repeated requests for a
particular tool without changing the entire mode. See
[Approvals and questions](../actions.md) for answering cards and handling expired
requests.
