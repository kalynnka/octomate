# Lark / Feishu

Lark connects over the SDK's **long connection**, a WebSocket the bot opens.
Messages and card actions arrive on it, so no request URL is needed.
Stopping Octomate shuts down the connection without retrying it.

## Create the app

In the [Feishu open platform](https://open.feishu.cn/app) or the Lark equivalent,
create a self-built app and:

1. Enable the **Bot** capability.
2. **Permissions**, from what the bot calls: sending and receiving messages
   (`im:message`, and the group and one-to-one receive permissions), reading user
   profiles (`contact:user.base:readonly`), uploading and downloading images
   (`im:resource`), listing the bot's groups and their members
   (`im:chat:readonly`), and CardKit for streaming cards. The permission checker names
   the exact scope when a call is refused.
3. **Event subscriptions**: choose the long-connection mode and subscribe to
   `im.message.receive_v1` and `card.action.trigger`.
4. Publish a version. The app id and secret from **Credentials** are `app_id` and
   `app_secret`.

## Configure

```yaml
tentacles:
  lark:
    type: lark
    app_id: cli_...
    agents: [inkling, claude]
```

```dotenv
OCTOMATE__TENTACLES__LARK__APP_SECRET=...
```

Two apps are two keys with `type: lark`, and two separate sets of threads.
Name agents you have already enabled, set `enabled: true` if this channel was
generated disabled, then check the configuration and restart Octomate. Open a
one-to-one chat with the bot or mention it in a group and verify a reply.

## Where things land

| Lark surface | In Octomate | The reply |
|---|---|---|
| A group message | A group chat room | A new thread under a message the bot posts |
| A message inside a thread | That thread, keyed on its root message | Replied into the thread |
| A one-to-one chat | A direct message, keyed on your open id | In the chat |

The group chat room follows the Lark group's name, refreshed on incoming root
messages. Agent work threads keep their own names.

A sub-thread is opened by sending the hint and replying under it. Lark identifies
a thread reply by its root message id, which is what the continuation is keyed on.


Teleport uses the current group or DM as the parent for a new sub-thread.
It does not offer nested sub-threads inside an existing thread. Entry from another
platform uses the linked account's one-to-one chat, or a group you and the bot are
both in: browsing Lark lists those groups, and a thread opened in one is read by
its members. Listing reads the members of every group the bot is in, so one group
Lark refuses to read fails the whole listing.

## Rendering

Everything is cards. The answer streams into a CardKit card with streaming mode on.
Each thinking block and each tool call is its own card, patched live and rewritten
as a collapsible panel when it finishes, titled with what it did or how long it
thought. Todos are one card patched as they change. Subagents get a card of their
own with their response in folded panels. A card Lark refuses to render falls back
to a plain message carrying the raw text.

[Artifact overviews](index.md#artifact-overviews) appear as native cards with a
blue title header, a provider subtitle and an **Open artifact** button in the same
chat or thread. The button opens the authenticated web viewer, where the app runs.

Approvals are one card per action with Approve and Deny; the click toasts and the
card is replaced with the resolution. Questions are one card holding the whole
batch: a dropdown for choices, which takes several picks when the question allows
them, a text field, and one Submit. Nothing reaches Octomate until Submit is
pressed, and Submit keeps working across a restart.

## MCP tools acting as the person

The same channel can offer Feishu's hosted MCP tools. It reuses the app id and
secret above; each user authorizes their own access. Once connected, agents can
use that person's connection from any channel.

Enable it on the existing channel:

```yaml
tentacles:
  lark:
    type: lark
    mcp: true
    mcp_tools: [get-user]
    oauth:
      callback_base_uri: https://octomate.example.com
```

```dotenv
OCTOMATE__OAUTH__ENCRYPTION_KEY=...
```

In the app's **Security Settings**, register the redirect URL described by
[`callback_base_uri`](../../api/config/channels.md#octomate.config.channels.LarkOAuthClientConfig).
Under **Permissions → User Identity Permissions**, enable the scopes requested
by `oauth.scopes`, then publish the app changes. The defaults allow looking up
user information and refreshing the authorization.

Choose names for `mcp_tools` from
[Feishu's supported remote MCP tools](https://open.feishu.cn/document/mcp_open_tools/supported-tools),
and add their required user scopes to `oauth.scopes` using
[Feishu's tool permissions](https://open.feishu.cn/document/mcp_open_tools/developers-call-remote-mcp-server).
The published catalog can lag behind the live service. After changing the list,
restart Octomate and check the tools offered by the connected MCP. Widening
scopes requires users to reconnect.

Each user connects from Trunkline or by asking an agent to install and connect
Lark. See [MCP setup](../../tentacles/mcp.md). This integration uses Feishu's
hosted endpoints.

Use OAuth scopes and Feishu's app and resource permissions to limit access.
The tool selection does not narrow the token's permissions for clients that
hold it themselves. See the [Lark OAuth adapter](../../api/mcp/oauth.md#octomate.tentacles.lark.oauth.LarkMcpBearerAuth)
for the custom header contract and its limits.

## Profile linking

From the chat: ask the agent to link your profile, and the one-time link arrives in
your one-to-one chat. With `oauth` configured, you can also link your Lark identity
from Trunkline's Profile page. Feishu verifies the user behind the app's token.
The authorisation card carries a link button only.

## Limits

- Card markdown has no code span, so backticks render literally; codes are shown in
  bold instead.
- A streaming card whose closing patch fails stays locked until Lark's own timeout,
  about ten minutes.
- Markdown is sent as one card without chunking.
- A reply to one of the bot's messages does not count as addressing it; mention it.
