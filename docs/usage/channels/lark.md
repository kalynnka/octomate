# Lark / Feishu

Lark connects over the SDK's **long connection**, a WebSocket the bot opens.
Messages and card actions arrive on it, so no request URL is needed.

## Create the app

In the [Feishu open platform](https://open.feishu.cn/app) or the Lark equivalent,
create a self-built app and:

1. Enable the **Bot** capability.
2. **Permissions**, from what the bot calls: sending and receiving messages
   (`im:message`, and the group and one-to-one receive permissions), reading user
   profiles (`contact:user.base:readonly`), uploading and downloading images
   (`im:resource`), and CardKit for streaming cards. The permission checker names
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

A sub-thread is opened by sending the hint and replying under it. Lark identifies
a thread reply by its root message id, which is what the continuation is keyed on.

## Rendering

Everything is cards. The answer streams into a CardKit card with streaming mode on.
Each thinking block and each tool call is its own card, patched live and rewritten
as a collapsible panel when it finishes, titled with what it did or how long it
thought. Todos are one card patched as they change. Subagents get a card of their
own with their response in folded panels. A card Lark refuses to render falls back
to a plain message carrying the raw text.

Approvals are one card per action with Approve and Deny; the click toasts and the
card is replaced with the resolution. Questions are one card for the batch, paged,
with a button per choice, a text field, and Back, Next and Submit. Buttons carry
enough state to keep working across a restart.

## Profile linking

From the chat: ask the agent to link your profile, and the one-time link arrives in
your one-to-one chat. Lark has no OAuth block for linking from Trunkline. The
authorisation card carries a link button only, because a confirm button would need
a card callback URL this deployment does not have.

## Limits

- Card markdown has no code span, so backticks render literally; codes are shown in
  bold instead.
- A streaming card whose closing patch fails stays locked until Lark's own timeout,
  about ten minutes.
- Markdown is sent as one card without chunking.
- A reply to one of the bot's messages does not count as addressing it; mention it.
