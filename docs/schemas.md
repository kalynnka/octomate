# Schema Reference

## Overview

All schemas live under `octomate/schemas/` and are organized into four modules:

| Module        | Purpose                                            |
| ------------- | -------------------------------------------------- |
| `conversation`| Agent-conversation and channel-address identity    |
| `user`        | Registered users and observed channel profiles     |
| `segments` | Message segment data types (text, image, at, etc.) |
| `events`   | Inbound events from IM platforms                   |
| `actions`  | Outbound actions toward IM platforms               |

Each module follows the ordering convention: **schemas → unions → TypeAdapters**.

## Data Flow

```
IM Platform  ──events/segments──▶  Tentacle  ──events/segments──▶  Agent
IM Platform  ◀──actions/segments──  Tentacle  ◀──actions/segments──  Agent
```

## Schemas

### conversation.py

A `Conversation` is an **agent conversation** — one agent's model context for a
thread — not a human chat log. The user-facing chat ledger is the `Thread` /
`ThreadMessage` rows owned by `ThreadManager`.

| Schema            | Type       | Direction             | Summary                                                              |
| ----------------- | ---------- | --------------------- | ------------------------------------------------------------------- |
| `ChannelAddress`  | dataclass  | internal              | Delivery address: tentacle + chat + sender user + platform thread   |
| `ConversationKey` | NamedTuple | internal              | Cache key for an agent conversation (channel address + owning agent) |

### user.py

| Schema        | Type                | Direction             | Summary                                                        |
| ------------- | ------------------- | --------------------- | -------------------------------------------------------------- |
| `User`        | Persisted transmuter | internal              | YAML-declared human with a stable username                     |
| `UserProfile` | Persisted transmuter | IM → Tentacle → Agent | Channel profile; optionally owned by a registered YAML user    |

### segments.py

| Schema             | Type         | Direction             | Summary                                               |
| ------------------ | ------------ | --------------------- | ----------------------------------------------------- |
| `Segment`          | Model (base) | —                     | Base class for all message segments                   |
| `TextSegment`      | Model        | IM ↔ Tentacle ↔ Agent | Plain text content                                    |
| `AtSegment`        | Model        | IM ↔ Tentacle ↔ Agent | Mention/@ a user                                      |
| `ImageSegment`     | Model        | IM ↔ Tentacle ↔ Agent | Image (local file path)                               |
| `MarkdownSegment`  | Model        | Agent → Tentacle → IM | Markdown-formatted text                               |
| `ReplySegment`     | Model        | IM ↔ Tentacle ↔ Agent | Quote/reply to a message                              |
| `FaceSegment`      | Model        | IM → Tentacle → Agent | Emoji face                                            |
| `RecordSegment`    | Model        | IM → Tentacle → Agent | Audio record                                          |
| `VideoSegment`     | Model        | IM → Tentacle → Agent | Video                                                 |
| `RpsSegment`       | Model        | IM → Tentacle         | Rock-paper-scissors                                   |
| `DiceSegment`      | Model        | IM → Tentacle         | Dice roll                                             |
| `ShakeSegment`     | Model        | IM → Tentacle         | Shake/nudge                                           |
| `PokeSegment`      | Model        | IM → Tentacle         | Poke action                                           |
| `AnonymousSegment` | Model        | IM → Tentacle         | Anonymous message marker                              |
| `ShareSegment`     | Model        | IM → Tentacle         | Shared link                                           |
| `ContactSegment`   | Model        | IM → Tentacle         | Contact card share                                    |
| `LocationSegment`  | Model        | IM → Tentacle         | Location data                                         |
| `MusicSegment`     | Model        | IM → Tentacle         | Music share                                           |
| `ForwardSegment`   | Model        | IM → Tentacle         | Forwarded message ref                                 |
| `NodeSegment`      | Model        | IM → Tentacle         | Forward node                                          |
| `XmlSegment`       | Model        | IM → Tentacle         | XML rich content                                      |
| `JsonSegment`      | Model        | IM → Tentacle         | JSON rich content                                     |
| `MessageSegment`   | Union        | IM → Tentacle → Agent | Discriminated union of all inbound segments           |
| `AgentSegment`     | Union        | Agent → Tentacle → IM | Discriminated union of segments the agent can produce |

### events.py

| Schema                 | Type         | Direction             | Summary                            |
| ---------------------- | ------------ | --------------------- | ---------------------------------- |
| `Event`                | Model (base) | IM → Tentacle         | Base for all events                |
| `MessageEvent`         | Model (base) | IM → Tentacle → Agent | Base for message events            |
| `PrivateMessageEvent`  | Model        | IM → Tentacle → Agent | Direct/private message             |
| `GroupMessageEvent`    | Model        | IM → Tentacle → Agent | Group chat message                 |
| `MessageEventUnion`    | Union        | IM → Tentacle → Agent | Private or Group message           |
| `NoticeEvent`          | Model (base) | IM → Tentacle         | Base for notice events             |
| `GroupUploadNotice`    | Model        | IM → Tentacle         | File uploaded to group             |
| `GroupAdminNotice`     | Model        | IM → Tentacle         | Admin set/unset                    |
| `GroupDecreaseNotice`  | Model        | IM → Tentacle         | Member left/kicked                 |
| `GroupIncreaseNotice`  | Model        | IM → Tentacle         | Member joined                      |
| `GroupBanNotice`       | Model        | IM → Tentacle         | Member banned/unbanned             |
| `GroupRecallNotice`    | Model        | IM → Tentacle         | Message recalled                   |
| `GroupCardNotice`      | Model        | IM → Tentacle         | Member card changed                |
| `GroupEssenceNotice`   | Model        | IM → Tentacle         | Message pinned/unpinned            |
| `FriendAddNotice`      | Model        | IM → Tentacle         | New friend added                   |
| `FriendRecallNotice`   | Model        | IM → Tentacle         | Friend message recalled            |
| `GroupPokeNotice`      | Model        | IM → Tentacle         | Poke in group/private              |
| `GroupLuckyKingNotice` | Model        | IM → Tentacle         | Red packet lucky king              |
| `GroupHonorNotice`     | Model        | IM → Tentacle         | Group honor change                 |
| `MsgEmojiLikeNotice`   | Model        | IM → Tentacle         | Emoji reaction on message          |
| `NotifyEventUnion`     | Union        | IM → Tentacle         | Poke / LuckyKing / Honor           |
| `NoticeEventUnion`     | Union        | IM → Tentacle         | All notice types                   |
| `FriendRequest`        | Model        | IM → Tentacle         | Friend request                     |
| `GroupRequest`         | Model        | IM → Tentacle         | Group join/invite request          |
| `RequestEventUnion`    | Union        | IM → Tentacle         | Friend or Group request            |
| `LifecycleEvent`       | Model        | IM → Tentacle         | Bot lifecycle (connect/disconnect) |
| `HeartbeatEvent`       | Model        | IM → Tentacle         | Heartbeat ping                     |
| `MetaEventUnion`       | Union        | IM → Tentacle         | Lifecycle or Heartbeat             |
| `EventUnion`           | Union        | IM → Tentacle         | Top-level union of all events      |

### actions.py

| Schema                 | Type        | Direction             | Summary                              |
| ---------------------- | ----------- | --------------------- | ------------------------------------ |
| `AgentMessage`         | Model       | Agent → Tentacle      | Outgoing message wrapper             |
| `ActionResponse`       | Model       | IM → Tentacle         | Response from IM after an action     |
| `SendGroupMsgParams`   | Model       | Agent → Tentacle → IM | Params for sending group message     |
| `SendGroupMsgAction`   | Model       | Agent → Tentacle → IM | Send message to group                |
| `SendPrivateMsgParams` | Model       | Agent → Tentacle → IM | Params for sending private message   |
| `SendPrivateMsgAction` | Model       | Agent → Tentacle → IM | Send message to user                 |
| `CallApiAction`        | Model       | Agent → Tentacle → IM | Generic API call action              |
| `ActionUnion`          | Union       | Agent → Tentacle → IM | Discriminated union of all actions   |
| `action_adapter`       | TypeAdapter | Agent → Tentacle → IM | Pydantic TypeAdapter for ActionUnion |
