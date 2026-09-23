# Start a conversation

Use your agent directly when you want to work in its own interface, or give it a
task through a channel and return when you are ready. You can use both approaches.

## Work through a channel

In Trunkline, start a new conversation, choose an agent and model, and send your
request. If the task involves project files, choose the project before the first
message. In another connected channel, message the bot directly or mention it in
a shared conversation.

> Compare these proposals, point out the tradeoffs, and recommend a next step.

Watch the reply in that conversation. You can leave the page while the agent
works; return to read the result or answer a request for your input. Work may
pause for an approval, so check pending requests if a task appears to have stopped.

Reply in the same thread to continue. Start a new thread for an unrelated task.
To change agents during the work, ask for a
[handoff](../gateway.md#summon).

## Keep using your own agent { #native-sessions }

After [connecting the client](../../installation/clients/quickstart.md), use your
agent in its app, editor or terminal as usual. Octomate calls these **native
sessions**; work started through a channel is a **driven session**.

Your native session keeps its own settings, tools and working directory. Open
Trunkline to read the collected conversation, or ask an agent to
[find it in your history](../history.md). To use Octomate's history and handoff
tools from that session, connect [Octomate MCP](../mcp/octomate.md) as well.

Reading a native session in Trunkline does not let you control the running agent.
To continue its work through a channel, ask the native agent to hand over the task
and relevant context. Its local process and files stay where they are.

## What to expect when switching

| Working directly with your agent | Working through a channel |
|---|---|
| Uses the machine and files where you started it | Uses the server and the selected project workspace |
| Keeps your agent's usual customisations | Uses the integrations configured in Octomate |
| You answer prompts in the agent's own interface | You answer supported prompts in the channel or Trunkline |

If a tool you use locally is missing in a channel conversation, check your
[MCP connectors](../mcp/proxy.md). Local plugins and MCP connections are not
inherited by channel work. DeepSeek Harness currently cannot use Octomate's tools
in driven sessions; use its native MCP connection or another enabled agent.

## If a session is missing

Send a fresh prompt after the server and client are connected, then check
Trunkline again. A session used while the server was unavailable is not collected
just by opening the console; continuing that session gives the client another
opportunity to send it. If it still does not appear, check the
[client setup](../../installation/clients/quickstart.md) for your agent.
