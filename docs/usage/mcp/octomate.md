# Octomate MCP

Octomate MCP lets your own agent use your shared history, reach connected channels
and work with the services you have added to Octomate. Once connected, ask for
these things in the same conversation where you normally work.

## Connect your agent

Follow the [client quickstart](../../installation/clients/quickstart.md) for your
agent, including its MCP setup, then restart the agent to load the connection.
The connection uses your Octomate account.

Try a simple request:

> Check Octomate and tell me which agents and destinations are available to me.

If Octomate's tools do not appear, revisit the MCP setup for that client. Session
collection and the MCP connection are separate; seeing a transcript in Trunkline
does not by itself mean the agent can use Octomate's tools.

## Find previous work

> Find our earlier discussion about the launch plan and read the decisions before
> updating this draft.

[History](../history.md) explains what the agent can search and how linking your
profiles makes conversations from other channels available.

## Continue through a channel

> Hand this task to an available agent in my connected channel. Include the plan,
> what is finished, and what still needs checking.

The receiving agent can continue from the brief. Your local agent keeps running
where it is, and local files are not transferred. See
[Moving a conversation](../gateway.md#from-a-native-session) for the available
ways to continue elsewhere.

## Use connected services

> Which services have I connected? Check whether the project tracker is ready,
> then use it to list my open tasks.

[Install and authorise an MCP connector](proxy.md) first. Your agent can discover
its tools and use them on your behalf. Give a clear request about what to read or
change, just as you would for any other tool.

## When you work through a channel

Driven Claude Code and Codex sessions receive Octomate's tools automatically;
Inkling has the same capabilities built in. You do not install a separate client
entry for those runs. Driven DeepSeek Harness does not yet have these tools.

## If your connection stops working

Check that the server is reachable and your client token is still valid. After
rotating a token, update the client configuration and reinstall its MCP entry,
then restart the agent. The [client guide](../../installation/clients/quickstart.md)
contains the commands.
