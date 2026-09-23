# NapCat

NapCat connects through OneBot: a forward WebSocket receives events and an HTTP
API sends messages. It supports group chats and DMs, with replies in the same chat.

!!! note
    NapCat is a community bridge for QQ. This channel is separate from the planned
    official QQbot integration.

## Create the app

Run the bridge using the upstream [NapCat Docker instructions](https://github.com/NapNeko/NapCat-Docker).
Keep its configuration and account data in persistent volumes so a container
replacement preserves them. The bridge may run on a different machine from
Octomate.

Follow the [NapCat WebUI guide](https://napneko.github.io/config/basic):

1. Open the WebUI on port `6099`, using the token from the container's startup
   logs. Log in to the account by scanning the QR code and complete the WebUI's
   password change if prompted.
2. Add and enable an **HTTP server** on port `3002` and a **WebSocket server**
   (forward WebSocket) on port `3001`. Use the `array` message format.
3. Set the same OneBot access token on both servers. This is separate from the
   WebUI token. Leave self-message reporting off.
4. Publish both API ports and make them reachable from Octomate. Inside the
   container, the servers must listen on `0.0.0.0` to accept published-port traffic.

The ports above are the choices used in the example below. Keep the API ports on
localhost or a private network. The repository's Compose file includes a
commented NapCat service example; it does not start the bridge until enabled.

## Configure

Add this block under `tentacles:` in `tentacles.yaml`, using an agent you have
already enabled:

```yaml
tentacles:
  napcat:
    type: napcat
    enabled: true
    agents: [inkling]
    ws_url: ws://127.0.0.1:3001
    http_url: http://127.0.0.1:3002
    mention_only: true
    stream:
      enabled: false
```

Supply the OneBot token through the environment or an optional `.env` file:

```dotenv
OCTOMATE__TENTACLES__NAPCAT__ACCESS_TOKEN=...
```

Use the bridge host's reachable address in both URLs if Octomate runs elsewhere.
Inside an Octomate container, `127.0.0.1` means that container; use the bridge's
service name on a shared Docker network or its host address instead.

[Check the configuration](../../installation/configuration.md#validate-without-starting)
and restart Octomate. Send a DM or mention the account in a group and verify that
the reply arrives in the same chat.

## Where things land

| NapCat surface | In Octomate | The reply |
|---|---|---|
| Group message | A group chat room | In the same group |
| Private message | A direct message | In the same DM |

NapCat's [message events](https://napneko.github.io/develop/event) distinguish
private and group messages. A quoted reply references a message; it does not
create a subthread. With `mention_only: true`, each group request needs an
`@`-mention. A quoted reply alone does not count as addressing the bot. DMs need
no mention.

## Rendering

Answers arrive as plain text when complete, with the latest todo checklist
appended. Mid-run notices can arrive separately. There are no streamed edits,
thinking cards or tool cards. Images and files are sent as native attachments;
inbound images are downloaded, while other inbound attachment types are ignored.

Approvals and questions appear as text only. There are no buttons or handlers to
submit a decision by replying in this channel. Use the pending request in
[Trunkline](trunkline.md) to respond.

## Profile linking

Ask from chat to [link your profile](../../installation/accounts.md#link-your-channel-profiles).
Octomate sends the authorization URL as plain text. A request made in a group
delivers the link to your DM; open it in a browser to complete linking.

## Limits

- Group chats and DMs only; no platform subthreads or guild channels.
- No interactive approval or question cards, and no streaming transport.
- Inbound voice, video, files and reaction events are not handled.
