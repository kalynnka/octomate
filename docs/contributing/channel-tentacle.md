# Adding a channel tentacle

A channel is a config model, a chromo, an ink, a tentacle class, and two lines of
registration. Nothing in the graph or the agents changes. Discord is the worked
example to read alongside this page: `octomate/tentacles/discord/`.

For runtime command discovery, authenticate the user and check that the channel enables the agent,
build a `CommandContext` from the observed surface and server-owned session, then
call the selected agent's `discover_commands(context)`. It uses the host's catalog
cache; callers must check agent enablement and resolve current context before each lookup,
including cache hits. Use only
the standard descriptor fields for presentation and pass a `prefix` for command
name completion. The [agent contract](agent-tentacle.md#runtime-commands) describes
catalog states, prerequisites and the equivalent authenticated HTTP endpoint.
Platform command controls and execution wiring are not implemented yet.

## 1. The config model

In `octomate/config/channels.py`, a variant of `ChannelConfig` with a `type`
literal and the platform's credentials, then add it to `ChannelConfigVariant`:

```python
class MyChannelConfig(ChannelConfig):
    """One connection to My Platform."""

    type: Literal["mychannel"] = "mychannel"
    bot_token: SecretStr
    stream: MyStreamConfig = Field(default_factory=MyStreamConfig)
```

If your platform streams well, give it its own `*StreamConfig` subclass with
`enabled = True` and a `flush_interval`, and annotate `stream` as that subclass so a
YAML override keeps its defaults. Export the class from `octomate/config/__init__.py`.

## 2. The chromo

A `Chromo[RawT, MessageT]` translates. `sip` turns a platform payload into a
`MessageEvent` or `None` for anything that is not a message; `outbound_markdown`
turns a reply into platform payloads, chunked to the platform's limit.

```python
class MyChromo(Chromo[MyRawEvent, MyOutboundMessage]):
    async def sip(self, raw: MyRawEvent) -> MessageEvent | None:
        return MessageEvent(
            message_id=raw.id,
            channel_thread_id=raw.thread_id,  # the platform's thread id, or None
            reply_id="",
            timestamp=raw.timestamp,
            user_id=raw.author_id,
            chat_id=raw.room_id,
            chat_type="thread" if raw.thread_id else "group",
            shared=True,  # can anyone besides the sender read this?
            segments=[TextSegment(data={"text": raw.body})],
            raw=raw.model_dump_json(),
        )

    def outbound_markdown(self, text: str) -> list[MyOutboundMessage]:
        return [MyOutboundMessage(content=chunk) for chunk in MY_CHUNKER.chunk(text)]
```

Three decisions live here and shape everything downstream:

- **`chat_type`** is `dm`, `group` or `thread`. A thread is a piece of work and
  keeps its conversation; a DM or group is a chat room and gets a sub-thread per
  kick. See [Threads and chat rooms](../usage/threads.md).
- **`shared`** is whether others can read the surface, independent of type. It
  drives the mention gate and every privacy decision in the gateway.
- **A reply's `user_id`**, if the platform tells you whom a message replies to, is
  what lets "reply to the bot" count as addressing it.

Override `outbound_segments` to ship images natively or encode a mention the
platform actually pings; the default flattens segments to text.

## 3. The ink

An `Ink[MessageT]` holds the platform client and does the I/O:

```python
class MyInk(Ink[MyOutboundMessage]):
    async def inspect(self) -> UserProfile: ...  # the bot itself
    async def get_user_profile(self, user_id: str) -> UserProfile: ...
    async def upload_media(self, data: bytes) -> str | None: ...
    async def download_image(
        self, seg: ImageSegment, message_id: str
    ) -> DownloadedImage | None: ...
    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[MyOutboundMessage],
        *,
        channel_thread_id: str,
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> IMMessageID | None: ...
    async def open_dm(self, user_id: str, opener: str | None = None) -> str | None: ...
```

`channel_thread_id` is the platform's destination id: the thread on a thread
surface, otherwise the chat. `reply_to` names a message inside it. A platform where
thread and reply are the same field collapses them here. `open_dm` returns `None`
when the platform has nowhere private; the gateway then refuses a scheme rather
than posting a one-time link to a group. An ink is an async context manager, so
open pooled clients in `__aenter__`.

## 4. The tentacle

```python
class MyTentacle(ChannelTentacle[MyRawEvent, MyOutboundMessage]):
    """My Platform over its bot API."""

    brand_color: ClassVar[Style | None] = Style(color="#123456", bold=True)
    thread_strategy: ClassVar[ThreadStrategy] = "flat_thread"
    surfaces: ClassVar[ChannelSurfaces] = ChannelSurfaces(
        sub_thread=True, direct_message=True
    )

    def __init__(self, id: str, octomate: Octomate, *, config: MyChannelConfig) -> None:
        self.ink = MyInk(config.bot_token)
        self.chromo = MyChromo()
        super().__init__(
            id=id, octomate=octomate, ink=self.ink, chromo=self.chromo, config=config
        )

    async def __aenter__(self) -> Self:
        await super().__aenter__()  # enters the ink and resolves the bot's profile
        # open the event socket; every inbound payload goes to self.ingest(raw)
        return self

    async def start_sub_thread(
        self, address: ChannelAddress, hint_text: str
    ) -> ChannelAddress:
        # post hint_text, open a thread under it, return the thread's address
        ...
```

`thread_strategy: "flat_thread"` means a message carrying a thread id continues
that thread with no routing; `surfaces` declares what the bot can open, which the
gateway consults before offering a spell. Claim the SDK's loggers through
`log_names` so its lines carry your colour. If the platform delivers events with
an acknowledgement deadline, run `ingest` as a detached task; a turn parked on an
approval must not hold the ack.

Gateway moves use `ChannelAddress` throughout. Implement
`Ink.suggest_addresses(address, source_address)` for optional discovery. The first
address carries the connected channel and linked user; the source is the current
conversation on this channel, or `None` when arriving from elsewhere. Return
addresses without creating chats or posting. The gateway caches these suggestions
per session; execution does not require discovery.

Implement `Ink.list_addresses(address, inside)` when the platform's places can be
browsed. It lists one level at a time, only when someone opens it: the top level
when `inside` is `None`, otherwise what a listed address's `metadata["inside"]`
names. Set `inside` on a place to open, such as a server, and leave it off an
address a thread can land in. List only what the linked user can see; where a
thread cannot land in one of those, say why in `metadata["barred"]` rather than
leaving it out. The base implementation refuses, which the gateway reports as its
reason. A listed address is a suggestion; execution still goes through
`prepare_address`.

What a channel knows for showing an address goes in `ChannelAddress.metadata`,
never in a wrapper around it: `name`, `inside` and `barred` are shared, and a
channel adds its own with a `TypedDict` extending `AddressMetadata` on its own
`ChannelAddress` subclass, as `DiscordAddress` does for `server`. Metadata is
excluded from equality and hashing, so a subclass with it is still the same
address.

Implement `Ink.prepare_address(address, source_address)` to validate one address
and return its platform-verified form. Check the parent type, requester and bot
access, and shared visibility. Refuse an unsupported address without opening a
thread. The base implementation refuses. `ChannelTentacle.prepare_address` checks
channel ownership and prevents nesting, then delegates platform work to Ink.
The gateway verifies the address's user against the requesting user's linked
identity through `thread_user_id(profile)`; Trunkline uses their registered
Octomate identity.

Slack and Lark support the current DM or group and the linked user's unresolved
DM. Discord validates a text-channel address directly; its discovery lists only
the source server's eligible channels, and its listing opens the servers you
share with the bot, then one server's channels. Trunkline prepares an independent private
thread. No target, location or landing wrapper is needed between these calls.

`start_thread(address, hint)` creates a thread at the prepared address and returns
its final address. The default resolves an empty DM `chat_id` through Ink before
calling `start_sub_thread` once. Trunkline creates a top-level thread; Discord
rechecks membership and permissions before opening. Keep client lookups,
permissions and API calls in Ink. Refuse a failed open rather than returning an
existing conversation. Both local and cross-channel gateway moves use this path.

`ingest` is the base class's and does everything from decoding to kicking the
graph, including the redelivery guard and the mention gate.

## 5. Register it

Add a `case MyChannelConfig()` to `build_channel` in `octomate/tentacles/channel.py`,
importing your module inside the case, and add the class to the channel arm of the
`match` in `octomate/app.py`. Restart, declare `type: mychannel` in
`tentacles.yaml`, send a message.

At this point the channel works with the default feelers: one message at the end
of a run, plain-text approval and question cards nobody can answer, a plain-text
OAuth link. The rest is drawing.

## 6. Feelers

Replace what your platform can do better. Assign a new `Feelers` in `__init__`
after `super().__init__`, keeping the defaults you do not replace.

- **Timeline.** Subclass `TimelineState` and override only the hooks you draw:
  `answer_delta` for streaming text through your ink's edit call, `tool_start` and
  `tool_end` for cards, `todo` for a checklist, `answer_segment` for native media.
  Honour the notice contract: a hook that renders reply text sets `noticed`, and a
  hook that opens a new entry calls `begin_entry` first. Implement
  `TimelineFeeler.open` to acquire the surface and set the message id on exit. Use
  a `TextStreamBatcher` fed from `config.stream` for pacing and a `StreamFlusher`
  so the drive loop never waits on the platform. [Feelers](../concepts/feelers.md)
  explains the pipeline.
  Override `actions_presented` only to flush output or indicate that input was
  requested. A pending action may leave the agent running; keep its thinking and
  answer surfaces open. The hook is serialized with stream events and cleanup.
- **Approvals and questions.** Subclass `ApprovalFeeler` and `QuestionFeeler`;
  `present` posts the cards and returns each action's platform message id. For the
  way back, decide whether the button carries state (Slack, Lark) or ids the
  handler reloads from the database (Discord), build a
  `DeferredActionBatchResponse`, and `kick` it only when the batch is complete.
  Refuse a press on a batch that is no longer pending.
- **OAuth.** Subclass `OAuthFeeler` and implement `send`; the base decides the
  private address.
- **Graph reports.** Present the events you are handed and never build one.
  Override `Feelers.present` only to draw a move or a failed turn as something
  other than a message.

## 7. Tests

Replay the canonical scenarios from `tests/support/scenarios.py` through your
timeline with `tests.support.channels.drive`, and assert on what your recording
ink sent. Add a live replay under `tests/trigger/test_mychannel.py` behind the
`trigger` marker for the surfaces a fake cannot verify. Then write the channel's
page under `docs/usage/channels/` and add its row to the render matrix; see
[Documentation](documentation.md).
