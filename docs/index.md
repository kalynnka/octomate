---
hide:
  - navigation
---

# Octomate

**Your agents. Your channels. Wherever you need them.**

Octomate brings your agents together into a personal assistant that works in the
background and stays within reach across channels. Set a task in motion and get on
with your day. When you return, the conversation can move with you: its **gateway**
can **teleport** it with its history to another connected channel, or hand the
work to another agent with the context to continue.

An agent can be part of an app, an editor or a terminal. Octomate connects agents,
channels and tools through **tentacles**, so where you talk to an agent can be
independent of where it runs and what it can do.

Research an idea, make a plan, create something or automate a task. Your agents
work on a machine you control, with shared history, tools and approvals. Your
apps, editor and terminal remain part of the workflow you already know.

[Let your agent set it up](installation/quickstart.md#agent-tldr){ .md-button .md-button--primary }
[Install on macOS](installation/macos.md){ .md-button }

## Keep the workflow you already like

**Run it on your own machine.** Keep the relay, history and working data on a
machine you control. Your existing apps and local workflow remain part of the
setup, and agents started by Octomate run on the server you choose.

**Reuse your agents and their logins.** Connect the agents you already use through
their supported integrations. Server-driven agents can reuse the runtime
authentication of the account they run under. You can give an existing agent a
new channel and shared tools without rebuilding it around a model API.

**Adopt it a piece at a time.** Start with session collection, add MCP tools, or
connect a channel. Continue using your apps, editor and terminal as usual. Each
tentacle adds a connection; you choose which parts of your workflow to bring
together. [Explore the available tentacles](usage/index.md).

Model requests go to the provider your agent uses, and connected channels receive
the messages sent through them.

## One set of agents, two ways to work { #two-ways-to-use-your-agents }

<div class="grid cards" markdown>

- **Keep your familiar workflow**

    *Native sessions*

    Use your agent in its own app, editor or terminal, with the settings and tools
    you already rely on. Octomate collects the session; its MCP connection adds
    shared history, tools and ways to hand off work.

    [Connect your agent](installation/clients/quickstart.md)

- **Delegate through a channel**

    *Driven sessions*

    Give an agent a task through a connected channel and let it work in the
    background. Octomate manages the run on your server and brings progress,
    results and requests for your input back to the conversation.

    [Connect a channel](usage/channels/index.md)

</div>

Use both as part of the same workflow: work directly with your agents when you
want to, and delegate through a channel when it suits you.
[Explore session behaviour and support](usage/agents/sessions.md).

## What the connections make possible

### Take the conversation with you { #move-work-to-the-right-agent-and-channel }

Start in one channel and continue in another without explaining the task from
scratch. The **gateway** finds the routes available to your linked profiles.
**Teleport** carries the same agent and its conversation history to a supported
destination. A **handoff** gives another agent a brief with the goal, decisions
and context it needs to take over.

As a task grows, you can give it a thread of its own or move it into a project's
workspace. Available moves depend on the agent and channel: a native session in
an app or terminal can hand off work, but Octomate cannot relocate that running
process. [Explore the gateway](usage/gateway.md) and
[threads and chat rooms](usage/threads.md).

### One history across your agents

"Why did we choose this approach?" should not depend on remembering which agent
you asked. Connected agent sessions and channel conversations join the same
history. You can revisit those conversations, and agents can search the recorded
message text through Octomate's tools.

Link your channel profiles to your Octomate account so the system recognises you
across channels. History tools search threads you participated in, including
messages that did not mention the bot. They do not expose every user's history or
search the contents of every tool call. [Explore history](usage/history.md).

### Connect an MCP service once, use it across agents

The **MCP proxy** makes your installed connectors available through one Octomate
connection. An agent can discover a connector, load the tools it needs, and call
them without a separate vendor configuration in every native client.

For OAuth connectors, each user connects their own account and keeps their own
grant. The operator can also offer connectors with a shared deployment credential.
Native agents use their Octomate MCP entry; supported driven agents receive the
tools from the server. [How the proxy works](usage/mcp/proxy.md).

### Keep decisions with the person doing the work

When a driven agent needs permission or an answer, Octomate presents the request
in the conversation. Supported channels let you approve, deny or answer there,
with the response recorded against the action. The agent's
permission mode determines which operations need approval.

This gating works alongside the runtime's own sandbox and permission system.
Channel support and restart behaviour vary by agent; see
[approvals and questions](usage/actions.md).

## Start here

!!! info "Deployment support"
    **macOS is the only deployment flow tested end to end.** Start with
    [Quickstart](installation/quickstart.md), or follow
    [Manual setup](installation/server.md) for another host. These docs follow the
    repository; check your installed version with `octomate --version`.

<div class="grid cards" markdown>

- **[Installation](installation/index.md)**

    Give your assistant the setup brief, or follow the macOS walkthrough. Then
    connect your native agent with hooks and MCP.

- **[Tentacles](tentacles/index.md)**

    Explore the available agents, channels and MCP connectors, and enable the
    ones you want to use.

- **[Usage](usage/index.md)**

    Learn how agents and channels share threads, history,
    approvals and workspaces, and how to move work between them.

- **[Concepts](concepts/index.md)**

    Why Octomate is a relay, how the graph routes a turn, and how feelers adapt it
    to each channel.

- **[Contributing](contributing/index.md)**

    Set up a development environment and add an agent, channel or MCP tentacle
    using the existing extension points.

- **[API Reference](api/index.md)**

    Look up settings, types and methods in the Python API, generated directly
    from the source and its docstrings.

</div>
