# ZCode tentacle

The `zcode` agent drives the runtime bundled with ZCode Desktop. It streams text,
reasoning and tool activity, records each turn in Octomate, and resumes ZCode's
own session on the next turn. It uses the same thread workspaces as Claude and
Codex. The integration targets ZCode Desktop 3.11.2 / bundled runtime 0.16.5.

## Setup

Install ZCode Desktop and Node.js. Configure an API-key provider in the desktop
app, then add this to your Octomate config home's `agents.yaml`:

```yaml
agents:
  zcode:
    provider: builtin:bigmodel
    models: [GLM-5.3]
    claims:
      GLM-5.3:
        ability: Repository-aware coding and software engineering with ZCode.
    permission_mode: build
```

Add `agent: zcode` and `model: GLM-5.3` to the desired channel's `agents` list in
`channels.yaml`, following its existing agent entries.

The default command is:

```text
node /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs app-server --stdio
```

`command` accepts a complete argument list if Node or ZCode is installed elsewhere.
No standalone CLI installation or UI automation is required.

`desktop_config` defaults to `~/.zcode/v2/config.json`. The provider identifier must
match an enabled provider in that file, and all configured models must belong to
it. Octomate reads its API key, endpoint and model settings at each run without
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
  Permission requests are denied and questions declined immediately. There are
  no approval/question cards. `yolo` retains ZCode's unrestricted tool behavior.
- Runtime preferences disable native-search enhancements, memory and automatic
  question answering. Other unsupported callbacks receive an explicit RPC error.
- Effort maps minimal/low to `low`, medium/high to `high`, and xhigh to `max`.
  Without an override, the desktop model's default is used. Unsupported effort
  levels fail explicitly.
- Inputs and outputs are text only. String instructions are carried in a marked
  section before the prompt. Structured output, attachments and deferred tool
  results are rejected.
- Gateway tools, native-session ingest and hooks are deferred. `gateway` is fixed
  to false. This does not install, change or grant trust to desktop hooks.
- Cancellation stops the session, records available partial history and closes
  the owned process group. Transport failures do not trigger retries. A future
  turn can resume the same persisted session.

## Verification

Focused tests cover protocol interleaving, callbacks, history reconciliation,
provider configuration, streaming, cancellation and fresh-process resume.
Live checks require the desktop installation and its configured provider: send
an initial prompt, follow up after the app-server exits, ask for read-only tool
activity and cancel an active turn. Confirm the same session identifier is used
and each Octomate run contains only that turn's messages.
