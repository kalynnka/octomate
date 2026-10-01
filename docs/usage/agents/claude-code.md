# Claude Code

`type: claude` drives Claude Code through the Claude Agent SDK as a local subprocess,
and records the Claude Code sessions you run yourself.

## Enable

Run Claude Code once as the server account and complete its login. Add the block
below to `tentacles.yaml`, bind `claude` in a channel's `agents` list, and
[check and restart Octomate](../../tentacles/index.md#enable-a-tentacle).
For your own native sessions, follow [Claude Code client setup](../../installation/clients/claude-code.md).

```yaml
tentacles:
  claude:
    type: claude
    permission_mode: default      # the SDK's own scale, handed over verbatim
    max_turns: ~
    approval_timeout: 3600        # seconds before an unanswered card expires
    instrument: false
    claims: {}
```

The harness supplies the model catalog: at startup Octomate asks the CLI for its
models and the account's provider, and keys each as `<provider>:<model>`, with
`anthropic` for a first-party login. Descriptions and supported efforts come from
the same call where the CLI reports them.

## Driven runs

A driven turn launches `claude` with the SDK in the thread's
[workspace](../workspaces.md), passing the project's `extra_roots` as additional
directories. Local customisation is off: the CLI runs in safe mode, so user
settings, hooks, plugins and skills do not load, and only the MCP servers Octomate
passes are mounted. That is one server, Octomate's own, built in process for this
turn, whose tools Claude lists as `mcp__octomate__<tool>`. Octomate's routing and
history instructions are appended to the system prompt.

The session id is settled before the CLI starts, and the same session is resumed
on every later turn of the conversation. Claude files a session under the directory
it ran in and resumes it only from there, so when a thread binds to a project
Octomate moves the transcript to the workspace's slot before resuming. One live
client per conversation: a new message while a turn is running interrupts it.

Effort maps directly onto the CLI's scale, except `minimal`, which becomes `low`.

## Runtime commands

The command catalog reads Claude's SDK initialization metadata without sending a
prompt. It preserves command names, descriptions and argument hints; aliases are
retained as Claude-specific metadata. Discovery requires an existing workspace
and uses its model, permission mode, project directories and saved session ID.
It does not create the workspace or persist an inspection session.

Discovery uses the same safe mode and strict MCP configuration as driven turns.
Only Octomate's tool-only MCP server can be mounted during a turn, so there are no
MCP prompts to add to the command catalog. Local commands, skills and plugins stay
disabled. With SDK 0.2.152 and bundled CLI 2.1.259, a test workspace command remained
absent even with `setting_sources=["project"]`; it appeared when safe mode was
removed. Omitting `setting_sources` uses the SDK's normal filesystem defaults;
safe mode is the restriction here. Loading local commands would require a separate
configuration opt-in for both discovery and execution, which Octomate does not yet
expose. See the [SDK command guide](https://code.claude.com/docs/en/agent-sdk/slash-commands).

The SDK caches initialization metadata per client. The host caches the catalog
until refresh, context changes, eviction or invalidation when a driven turn starts
or the tentacle starts or stops. Refresh opens a new inspection client to obtain a
new runtime snapshot. Missing command metadata reports unsupported; an empty
command list is a successful empty catalog.

Explicit execution sends the selected native command through SDK `query()` with
the raw argument text and the conversation's saved session. The execution client
checks its own initialization metadata before submitting the command; a removed
or changed entry requires a refresh and is never sent as an ordinary prompt.
Commands absent from the catalog are rejected. An advertised command that needs
a terminal returns the runtime's refusal text.

Direct output is saved on the command receipt without creating a model run.
Commands that start model activity use the normal streaming, approval and run
history path. SDK error results fail the receipt; successful direct text appears
once. Attachments are not supported.

`/clear` updates the conversation's resumable session ID from the SDK's final
result. `/compact` leaves summarization to Claude. Neither command deletes or
rewrites Octomate's recorded history. Native settings commands affect their SDK
session; they do not change Octomate's saved model or permission settings, which
are applied again on the next turn.

## Approvals and questions

Two bridges, both landing on the same cards:

- **Tool permission.** The SDK asks Octomate before each tool the posture does not
  already allow. Octomate raises an approval, waits up to `approval_timeout`, and
  answers allow or deny. "Allow for session" is remembered on the conversation. A
  commissioned run with no user to ask is denied at once, with a message telling the
  agent to proceed another way.
- **Questions.** Claude's `AskUserQuestion` tool is intercepted by a hook. Since a
  hook can only allow or deny, the user's answer travels back as the reason for a
  deny, which Claude reads as the answer. Choices are capped at three.

The bridge parks the live SDK client while it waits, so an answer is not durable
across an Octomate restart: restart mid-question and the run is gone, though the
conversation resumes on the next message.

## Native sessions

`octomate claude hooks install` registers `UserPromptSubmit`, `Stop`, `SessionEnd`,
`SubagentStart` and `SubagentStop`, and launches a tail on each prompt. The hooks
sketch the turn live; the tail then replaces the sketch with the full run from the
transcript, subagents included, each subagent as its own conversation under the
same thread. A session's own title becomes the thread's name. Permission mode
changes are observed and recorded, never set.

Resuming a driven session natively skips already recorded driven turns when their
runtime identity is available. See [switching sessions](sessions.md#what-to-expect-when-switching)
for where new turns land and the limits for older history.

What the tail skips: transcript line types it does not model, and inline subagent
relics from transcripts older than the per-file subagent layout.

## Not yet

- **Remote hosts.** An `ssh` block is parsed and warned about, not honoured. Runs
  stay local until workspaces can be forked on another machine.
- **Structured output** returns whatever the CLI produced, validated once, with no
  retry loop.
- **Images in a prompt** are dropped; the driven prompt is text.
