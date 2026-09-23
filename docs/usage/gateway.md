# Moving a conversation

Ask your agent to continue somewhere else or bring in another agent. Tell it
where you want to go and what needs to carry forward; it can check the available
agents and destinations for you.

> Where can we continue this conversation, and which agents are available there?

You do not need to know the gateway tool names. They appear below so you can
recognise them in the agent's activity.

## Ask another agent to take over { #summon }

Use a handoff when you want a different agent to own the task and its follow-ups.

> Hand this to Codex for implementation. Include the plan we agreed on and ask it
> to show me the changes before committing.

The receiving agent gets a brief with the goal and relevant context. Be explicit
about decisions it must preserve. Depending on the channel, the handoff can stay
in the thread, open a new thread or go to your private conversation elsewhere.
The agent can only choose from the agents enabled at the destination.

The tool for this is **summon**. A handoff does not transfer a running process or
copy files from your machine.

## Continue with the same agent elsewhere { #teleport }

Use **teleport** when you want the same agent and conversation history in another
supported destination.

> Continue this in my private conversation on the other connected channel.

First [link your profiles](../installation/accounts.md#link-your-channel-profiles)
so Octomate can find you there. Available destinations depend on the channel;
ask the agent to list them if your requested move is unavailable.

A conversation can cross into another channel's private messages only when its
current conversation is already private. For a task that began in a group,
[continue privately with a brief](#scheme) instead. Native sessions cannot be
teleported; use a handoff from your local agent.

You can also ask the agent to use a project before starting file work:

> Continue this task in the website project, starting from the release branch.

See [Projects](projects.md) for choosing a project and starting point.

## Take a group request into private messages { #scheme }

> Let's discuss the details in my direct messages.

The agent can move the task out of the group with a brief for the agent answering
your DMs. This is **scheme**. Continue in the private conversation that appears;
it may be handled by a different agent. This option needs a channel with direct
messages.

## Send a result without moving the conversation { #send }

> Send me the summary privately, then keep working on the next step here.

The agent can send progress, a result or a file to a supported destination while
your current conversation continues. This is **send**; it does not hand the task
to another agent.

## Delegate a smaller task

With Inkling, you can ask it to delegate a self-contained task and bring back the
report while it remains your main point of contact.

> Ask another available agent to review this proposal, then compare its findings
> with your own.

Delegated work cannot stop to ask you for approvals or answers. Give it enough
context and a task it can complete with the access already available. If it needs
your involvement, use a handoff instead.

## Finish project work { #dispel }

Once you have reviewed and delivered the result, you can ask the agent to release
the workspace. Use this when the task is finished, rather than for an ordinary
pause. See [Workspaces](workspaces.md#finish-the-task).

## From your own agent { #from-a-native-session }

With [Octomate MCP](mcp/octomate.md) connected, your native agent can find
available destinations, hand off work and send messages to a channel. Include
important context in the request. Work on local files stays on your machine;
choose a registered project if the receiving agent needs to work on server files.
