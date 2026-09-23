# QQ through NapCat

!!! warning "Unverified"
    NapCat is a community reimplementation on top of NTQQ rather than a vendor SDK,
    and this channel has not been exercised in a while. It sends and receives
    messages; it cannot stream, open threads, or take an answer to an approval or a
    question.

Octomate is the **client** here: it dials NapCat's OneBot WebSocket for events and
calls its HTTP API for actions.

## Run NapCat

The checkout's `docker-compose.yml` has a profile for it:

```sh
docker compose --profile qq up -d
```

Log the QQ account in through NapCat's web UI on port 6099, then enable a
WebSocket server on 3001 and an HTTP server on 3000 in its network settings, with
an access token if you want one.

## Configure

```yaml
tentacles:
  napcat:
    type: napcat
    agents: [inkling]
    ws_url: ws://127.0.0.1:3001
    http_url: http://127.0.0.1:3000
    backoff_base: 1
    backoff_max: 60
    backoff_factor: 2
```

```dotenv
OCTOMATE__TENTACLES__NAPCAT__ACCESS_TOKEN=...
```

A dropped socket is reconnected with exponential backoff between the two bounds.

## What works

- Group messages become a group chat room, private messages a direct message.
  There is no thread concept, so a reply lands back in the same chat, quoting the
  message it answers.
- Text, mentions, images and quotes are understood inbound; outbound markdown is
  stripped to plain text, and images travel inline as base64.
- The whole answer is sent once at the end, with a todo checklist appended if the
  agent kept one.
- Approvals and questions are shown as text with the action's id, and there is no
  way to answer them from QQ. Use a permission mode that does not ask, or answer
  from Trunkline.
- Profile linking works from the chat: the link is sent to the private chat.
