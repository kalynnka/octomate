---
hide:
  - navigation
---

# Octomate

**Relay and collect every chat you have with a coding agent, whichever harness
made it. Spread tentacles out to wherever you already work. Offer that history and
those tools wherever you want them.**

Keep running Claude Code, Codex or DeepSeek Harness the way you already do. Octomate
records each session from its hooks and transcript, makes it a thread you can read
and continue from Slack, Lark, Discord, QQ or its own web console, and gives every
agent the same tools: routing to a better-placed agent, searching what was said,
and the MCP connectors you authorised.

!!! note "Early software"
    The architecture and APIs are still moving. These pages describe the source on
    `main`; an installed release may lag. Check `octomate --version`.

<div class="grid cards" markdown>

- **[Installation](installation/index.md)**

    Stand up the server on macOS, Linux, Windows or Docker, create your account, and
    connect the agents you run. Or hand the [brief](installation/agent-setup.md) to
    your assistant.

- **[Usage](usage/index.md)**

    The tentacles: four agents, five channels, and the MCP connectors. Then what
    they share: threads, moving a conversation, approvals, workspaces, history.

- **[Concepts](concepts/index.md)**

    Why it is a relay, how the graph decides, what a feeler is, and how the
    workspace design keeps an agent's work without keeping its disk.

- **[Contributing](contributing/index.md)**

    Set up, check, and add a tentacle of your own from a skeleton with the real base
    classes.

</div>

## Three things, in that order

**Collect.** Claude Code, Codex, DeepSeek Harness, or a run you drove from chat:
every turn lands in one record, including the sessions you start in your own
terminal. Nothing about how you work changes; one command per harness installs the
hooks.

**Spread.** The same thread reaches Slack, Lark, Discord, the console and QQ,
rendered natively on each: streaming text, tool cards, todo lists, approval
buttons. Threads are independent, so a long run does not block the next question.

**Offer.** That history, and the tools built on it, are available from any of those
surfaces: searchable mid-run, resumable later, handed to a different agent when the
one that started is not the one that should finish.

## Two ways in

| | Native session | Driven session |
|---|---|---|
| Starts | In your terminal or editor | From a message on a channel |
| Runs | On your machine, with your login | On the server, in a git workspace |
| Gets | Recorded, and Octomate's tools over MCP | Octomate's tools directly; local customisation off |

## Approvals are actions, in batches

One action is exactly one thing you are asked: one approval, or one question.
Everything a turn waits on arrives together, and each action records who answered
it and when. Actions are persisted before they are asked, so a restart in the middle
of a batch still lands the run where it left off.
