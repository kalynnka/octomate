# TL;DR for your agent

Give the following brief to an assistant with access to the computer you want to set
up. Fill in the choices you already know. The assistant should read the linked pages
for the installed version and report each verification result.

```text
Help me install Octomate using its CLI and these docs:
https://kalynnka.github.io/octomate/installation/

Goal: a running server, my own registered account with a scoped API token,
native-session hooks, and Octomate's MCP entry in the coding agent I use.

First establish with me:
- Server OS, and whether this computer is also the client.
- A new installation directory, kept apart from any source checkout.
- Which agents to enable: Claude Code, Codex, or DeepSeek Harness (experimental).
- Which channels to enable. Trunkline (the web console) is how I will register
  and issue my token, so enable it.
- Whether access is local only or through an existing HTTPS origin.

Install the operator CLI standalone: `uv tool install octomate-cli`.
Run `octomate --version` and `--help` before applying any instruction.

On macOS: `octomate service init --prepare`, then complete the generated
CONFIGURATION.md. Preparation writes files only. The database, my account,
the LaunchAgent and the first live request are separate steps in the macOS
guide. The service runs only while my desktop account is logged in.
On Linux: follow the server guide and the systemd recipe.
On Windows: everything runs inside WSL 2, the client included.
Do not invent service commands for platforms the CLI does not support.

All agents, channels and MCP connectors are declared under `tentacles:` in
`config/tentacles.yaml`, keyed by an id with a `type`. A channel's `agents`
is a list of agent ids; the first answers by default.

Keep secrets in `.env` (OCTOMATE__TENTACLES__<ID>__<FIELD>), never in YAML or
in a message to me. A YAML value overrides `.env`, so remove any placeholder
you replace. Do not enable telemetry, bypass approval prompts, or bind to a
non-loopback address unless I ask.

Set OCTOMATE_HOME and OCTOMATE_DB_URL explicitly, print the resolved database
path, and ask me before touching an existing database or replacing an
existing service or configuration. Preserve auth salts and any OAuth
encryption key; they cannot be regenerated without losing sessions and
stored tokens.

Use the harness login of the account the server runs under. Do not copy my
credentials anywhere or create a setup token to work around a login.

Then: issue an invitation, register me in the browser, issue an API token
with `hooks` and `mcp` scopes, save it with `octomate configure`, and install
only my chosen runtime's hooks and MCP entry on the machine that runtime
runs on. Reinstall MCP after any change to the URL or token.

Verify: configuration validates, the service starts, sign-in works, a
native prompt and its answer arrive in Trunkline, one driven reply comes
back on a channel, `gateway_scry` works over MCP, and unauthenticated calls
to /hooks and /octomate/mcp get 401. Restart and verify again.

Finish with a report: paths, versions, the service commands I will use, what
passed, and what is still unverified. No secret values in the report.
```

## What your agent will need from you

Harness logins, bot application setup on Slack, Lark or Discord, invitations, and
OAuth consent involve your identity. Supply secrets through the host's private files
or the platform's own login screen. A generated placeholder is not a credential.

On an always-on Mac, use the desktop account that owns the harness login and its
Keychain. A GUI LaunchAgent runs while that account is logged in, so a Mac that has
rebooted to its login screen has not restored the service yet. See
[macOS](macos.md#activate-the-gui-service).
