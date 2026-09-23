# Quickstart

Copy the brief below into a coding agent with access to the machine you want to
use. It covers the server, your account, and the hooks and MCP entry on your client.

!!! info "Deployment support"
    **macOS is the only deployment flow tested end to end.** To do the setup
    yourself, follow [Install on macOS](macos.md). [Manual setup](server.md)
    explains the configuration and foreground server setup for other hosts.

## Agent TL;DR

```{ .text .copy title="Copy this instruction to your agent" }
Help me set up Octomate so I can keep using my coding agents normally,
collect their sessions, and use them from chat.

Read the installation docs first:
https://kalynnka.github.io/octomate/installation/
Source: https://github.com/kalynnka/octomate
Use instructions that match the installed release. Inspect --version and
--help when a command differs from the guide.

Establish the choices you cannot infer from this machine:
- Where the server will run, and where I run my native agents.
- A new, separate installation directory.
- Which of Claude Code, Codex or DeepSeek Harness I use.
- Local-only access or private access through Tailscale or an existing VPN.
Start with my chosen agent and Trunkline, the web console. Add external chat
channels and MCP connectors after the first working session, if I want them.

Install the standalone operator CLI with `uv tool install octomate-cli`.

macOS (the tested deployment flow):
- Work as the desktop user who owns the harness login, without sudo.
- Use `octomate service init --prepare` to create the installation.
- Follow its CONFIGURATION.md and the macOS guide. Set cookie_secure=false
  for loopback HTTP, build Trunkline, and set its static_dir.
- Validate the configuration and resolved database path before migration.
- Initialise the new database, run in the foreground, create my account,
  then review and install the generated GUI LaunchAgent.
- Explain that the service needs a logged-in desktop account after reboot.

Other hosts (not tested end to end):
- Follow Manual setup. The interactive wizard rejects non-macOS hosts.
- Reuse its configuration generator from the installed checkout as the
  manual guide describes; it writes files without starting a service.
- Review the settings one by one using the packaged YAML templates.
- Verify the foreground server before arranging service supervision.
- Record any platform-specific steps that remain unverified.

Keep OCTOMATE_HOME and OCTOMATE_DB_URL explicit. Inspect the resolved paths.
Preserve existing config, databases, auth salts and OAuth keys. Ask before
replacing an installation or migrating an existing database.
Provide secrets in YAML or the service environment; .env is optional.
Keep secret values out of chat, logs and the final report.
If using .env, remove matching YAML placeholders: YAML takes precedence.
Keep loopback binding and the normal approval modes for the first run.
Do not expose the server to the public internet. Its built-in login is
intended to accompany private network access.
Use the service user's harness login; let me complete login and consent.

After the server works:
1. Issue an invitation with the server's CLI. Let me register in Trunkline.
2. Let me issue an API token with hooks and mcp scopes in Account.
3. On each native agent machine, save the URL/token with octomate configure.
4. Install that runtime's hooks and MCP entry, preserving other settings.
   Explain their scope. For Codex, have me trust the hooks in /hooks.
5. Restart the native runtime to load its settings.

Verify and report the actual results:
- Anonymous requests to the enabled hook, console API and MCP return 401.
- Browser sign-in survives a reload.
- A fresh native prompt AND its answer appear in Trunkline.
- The native agent calls gateway_scry with reveal="routes" over MCP.
- A new Trunkline conversation gets a driven reply from my chosen agent.
- The service restarts and stored history remains available.

Finish with versions, paths, service commands and passed/unverified checks.
Do not describe session collection as control of my running terminal.
```

## What you supply

The agent can prepare files and explain each step. You supply your harness login,
complete browser registration, and authorise any services you connect. A local
installation with Trunkline needs no chat-platform bot credentials or OAuth app.

On an always-on Mac, use the desktop account that owns the harness login. Its GUI
LaunchAgent runs while that account is logged in. After a reboot, the login screen
alone is not enough to bring the service back.

## Prefer to deploy by hand?

Start with [Requirements](requirements.md), then [macOS](macos.md). That walkthrough
uses the same CLI preparation and takes you through configuration, the first run,
account creation and service activation.

For another host, use [Manual setup](server.md). For access from other devices,
see [Tailscale](networking.md#tailscale). If the server already exists, go straight to the
[client quick start](clients/quickstart.md).
