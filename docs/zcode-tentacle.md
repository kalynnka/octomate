# ZCode tentacle

The `zcode` agent drives the runtime bundled with ZCode Desktop. It streams text,
reasoning and tool activity, records each turn in Octomate, and resumes ZCode's
own session on the next turn. It uses the same thread workspaces as Claude and
Codex. The integration targets ZCode Desktop 3.11.2 / bundled runtime 0.16.5.
The official `zai-sdk` supplies GLM inference, not the ZCode session and tool
runtime; this tentacle uses the desktop app-server protocol.

## Setup

Install ZCode Desktop and Node.js. Configure an API-key provider in the desktop
app, then add this to your Octomate config home's `tentacles.yaml`:

```yaml
tentacles:
  zcode:
    type: zcode
    provider: builtin:bigmodel
    claims:
      GLM-5.3:
        ability: Repository-aware coding and software engineering with ZCode.
    permission_mode: build
    approval_timeout: 3600
```

Add `zcode` to the desired channel's `agents` list in `tentacles.yaml`, following
its existing agent entries. Select a discovered ZCode model in the web UI. Routes
use provider-qualified identifiers such as `builtin:bigmodel:GLM-5.3`. Existing unqualified model names
remain accepted for direct runs and claim overrides.

The default command is:

```text
node /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs app-server --stdio
```

`command` accepts a complete argument list if Node or ZCode is installed elsewhere.
No standalone CLI installation or UI automation is required.

`desktop_config` defaults to `~/.zcode/v2/config.json`. The provider identifier must
match an enabled provider in that file. Octomate discovers all models configured
for that provider through `workspace/readState` during startup. It selects each
model in turn within one app-server process, since the runtime advertises models
as they are selected, and verifies the complete catalog before advertising routes.
Model names are not restricted to a fixed list.
Octomate reads its API key, endpoint and model settings at each run without
modifying the file. The API key travels only to the local child over stdin; it
does not become a command argument or Octomate ledger metadata. Desktop-login
providers requiring authentication callbacks are unsupported in this version.

`state_dir` defaults to `~/.octomate/zcode`. The child receives it as
`ZCODE_STORAGE_DIR`, and `ZCODE_SESSION_DB_PATH` names `sessions.db` inside it.
Keep that directory across restarts: Octomate's conversation `external_id` refers
to a session stored there. A missing session fails explicitly instead of starting
a fresh conversation. ZCode's runtime remains responsible for its standard user
configuration, plugins and tool behavior.

## Behavior and limitations

- One app-server process runs per active turn. Turns in the same conversation
  serialize; different conversations can run independently.
- `build` is the default posture. Conversations and projects can select ZCode's
  `plan`, `build`, `edit`, `yolo` or `auto` modes through existing permission APIs.
  Requests needing human review use the channel's existing approval and question
  cards. `yolo` retains ZCode's unrestricted tool behavior.
- Approve or deny a tool request, or allow that tool for the Octomate conversation.
  Conversation grants survive app-server restarts; they do not write ZCode's
  project-wide permission rules. Questions always require their own answer.
- Questions offer up to three suggested choices and accept free text. Additional
  choices and descriptions appear in the hint. For multiple selections, enter
  the answers as text. Plan-review cards include the full plan; approving starts
  implementation, while written feedback returns to ZCode for revision.
- `approval_timeout` defaults to 3600 seconds for both approvals and questions;
  `null` waits indefinitely. Timed-out cards expire and their requests are
  declined. Non-interactive runs and runs without a channel decline immediately.
  Waiters live in the current backend process, as with Claude and Codex; an
  interrupted waiter is not resumed across a backend restart. Cancellation,
  shutdown and transport failure expire outstanding batches.
- Runtime preferences disable native-search enhancements, memory and automatic
  question answering. Other unsupported callbacks receive an explicit RPC error.
- Effort maps minimal/low to `low`, medium/high to `high`, and xhigh to `max`.
  Routes advertise only the levels supported by the runtime catalog; configured
  claims can restrict that set further. Without an override, the desktop model's
  default is used. Unsupported effort levels fail explicitly.
- Inputs and outputs are text only. String instructions are carried in a marked
  section before the prompt. Structured output, attachments and deferred tool
  results are rejected.
- Gateway tools, native-session ingest and hooks are deferred. `gateway` is fixed
  to false. This does not install, change or grant trust to desktop hooks.
- Setup and rejected submissions do not record an unsent prompt or advance its
  source cursor. A confirmed preflight failure after acknowledgement ends the run
  without a generation timeout. Control commands and hook-blocked prompts can
  complete without a native prompt message. The legacy `prompt_completed` state
  notice acknowledges admission; the runner waits for the actual terminal turn
  event before reconciling and recording a completed response.
- History retrieval starts after the pre-turn tail, retaining the entire current
  turn. Missing prompt boundaries fail explicitly. Hidden synthetic context and
  discarded assistant attempts are excluded from canonical conversation history.
  Visible assistant responses retain their text, reasoning, tools and usage even
  when their origin is `agent_runtime`; hidden/debug context and native timeline
  markers remain excluded.
  Native errors and finish reasons are preserved. Individual protocol frames over
  16 MiB fail with an explicit size error.
- Correction of already displayed text after native stream recovery remains
  deferred.
- Cancellation stops the session, records available partial history and closes
  the owned process group. Transport failures do not trigger retries. A future
  turn can resume the same persisted session.

## Verification

Focused tests cover protocol interleaving, repeated callbacks, card decisions,
question answers, history reconciliation, provider configuration, streaming,
cancellation and fresh-process resume with conversation grants. Regression cases
also cover discovery, malformed event variants, preflight failure, no-prompt turns,
history visibility and finish reasons, and child processes that outlive their parent.
Live checks require the desktop installation and its configured provider: send
an initial prompt, follow up after the app-server exits, ask for read-only tool
activity and cancel an active turn. Confirm the same session identifier is used
and each Octomate run contains only that turn's messages. In a temporary workspace,
approve and deny harmless gated tool requests, answer a question, and cancel a
run waiting on a card. Confirm repeated native announcements present one batch.
