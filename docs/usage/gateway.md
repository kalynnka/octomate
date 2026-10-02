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

Teleport requires tool approval, handled by the agent's harness according to its
configured permissions. Automatic review, bypass modes and existing grants keep
their normal behavior. When the harness asks for a person, Octomate relays that
request through the existing approval or question cards. Denying approval prevents
the tool from recording a move. Trunkline's direct Teleport action keeps its
existing submission flow.

First [link your profiles](../installation/accounts.md#link-your-channel-profiles)
so Octomate can find you there. Available destinations depend on the channel;
ask the agent to list them if your requested move is unavailable.

Each channel creates a thread at the selected address. Trunkline opens a new
private thread, including when the source is already in Trunkline. Slack and Lark
open a sub-thread under the current DM or group, or under your linked DM when
arriving from another platform. Existing threads cannot contain new sub-threads.
Discord suggests eligible text channels in the source server, without enumerating
all servers. Discord DMs and NapCat cannot host an isolated thread.

Teleport and Summon accept a `destination` address and a `new_thread` flag, which
defaults to true. A null destination uses the current conversation's address.
The address uses the same fields as the conversation address:
connected channel, chat type, chat ID, user ID and optional thread ID. Discovery
suggests addresses; it is not an allowlist. To choose an agent for an address,
use `inspect` with `reveal="routes"` and its `channel_tentacle_id` as `channel`. The channel validates each submitted
address before creating anything, and the user must match your linked identity.

A known Discord text channel can be selected directly, including from a private
conversation on another platform. Its address has `chat_type="group"` and the
text-channel ID in `chat_id`. Discord checks membership and thread permissions;
a guild ID, DM, existing thread or forum is refused. Slack and Lark currently
support their default DM and the current parent, rather than arbitrary chat IDs.

With `new_thread=false`, Summon takes over the current conversation in place.
Teleport uses that setting only to bind the current thread to a project.
Neither operation reuses an unrelated existing conversation. A failed thread
creation does not silently turn into an in-place operation.

Teleport requires independent history copying: driven Codex, Claude and Inkling
support it; DeepSeek does not yet. In Trunkline, an owned native Codex session can
also teleport using its latest fully uploaded completed turn. The import keeps
that turn's model and permissions, copies its transcript, and leaves the native
source usable. The selected destination must expose a Codex agent; its first
compatible configured route handles the import. Other connected Codex agents
do not prevent the move.

Native teleport checks transcript ownership, the completed turn, its permissions
and import metadata before opening or announcing a destination. Missing or
unusable history is refused without creating a thread.

A conversation can cross to a new destination only when its current conversation
is private. For a task that began in a group,
[continue privately with a brief](#scheme) instead. An anonymous native MCP call
cannot teleport its history; select the uploaded session in Trunkline, or use a
handoff from your local agent.

You can also ask the agent to use a project before starting file work:

> Continue this task in the website project, starting from the release branch.

See [Projects](projects.md) for choosing a project and starting point.

## Take a group request into private messages { #scheme }

> Let's discuss the details in my direct messages.

The agent can move the task out of the group with a brief for the agent answering
your DMs. This is **scheme**. Continue in the private conversation that appears;
it may be handled by a different agent. This option needs a channel with direct
messages. Its destination is `{"kind": "dm"}` for this channel, or
`{"kind": "dm", "channel": "discord"}` for your linked account on another channel.
The recipient comes from your linked profile. Moving into the current channel's
DM requires a shared source; another channel's linked DM can also be selected
from a private conversation.

## Send a result without moving the conversation { #send }

> Send me the summary privately, then keep working on the next step here.

The agent can send progress, a result or a file to a supported destination while
your current conversation continues. This is **send**; it does not hand the task
to another agent. It accepts `{"kind": "here"}` or the same DM targets as Scheme.
When choosing a DM from an inspected address, use its `channel_tentacle_id` as the
target's `channel`. Native sessions without a current conversation must name that
channel explicitly.

## Delegate a smaller task

With Inkling, you can ask it to delegate a self-contained task and bring back the
report while it remains your main point of contact.

> Ask another available agent to review this proposal, then compare its findings
> with your own.

Delegated work cannot stop to ask you for approvals or answers. Give it enough
context and a task it can complete with the access already available. If it needs
your involvement, use a handoff instead.

## Finish project work { #dismiss }

Once you have reviewed and delivered the result, you can ask the agent to release
the workspace. Use this when the task is finished, rather than for an ordinary
pause. See [Workspaces](workspaces.md#finish-the-task).

## From your own agent { #from-a-native-session }

With [Octomate MCP](mcp/octomate.md) connected, your native agent can find
available destinations, hand off work and send messages to a channel. Include
important context in the request. Work on local files stays on your machine;
choose a registered project if the receiving agent needs to work on server files.
