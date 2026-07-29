# Plan (rough): DM + cross-channel continuation

> **Status:** §1 shipped, as a spell rather than a destination; §2 still parked ·
> **Owner:** @luhui · **Created:** 2026-07-06 · **Updated:** 2026-07-28
> **Builds on:** [done/self-routing-dispatch.md](done/self-routing-dispatch.md) — unparks its
> two deferred `gate` destinations. **Reference:** [cancelled/channel-retargeting.md](cancelled/channel-retargeting.md) §0b.

The shipped `gate` (`GatewayCapability`, [gateway.py](../../octomate/capabilities/gateway.py))
routes a turn only to **local** surfaces — `here` (current thread) and `thread` (a sub-thread of
the current chat) — because `summon`/`teleport` open every surface through `start_sub_thread`,
which stays on the same channel + `chat_id`. The gate has since gained `commission`/`whisper`
(accomplice spells); they are orthogonal here. Two destinations were parked. This is a rough
scoping of what each **requires** and what must exist **first** — §1 shipped differently, see
below.

## 1. Continue 1:1 in the user's DM — **shipped as the `scheme` spell**

Not a `summon`/`teleport` destination, as scoped below. Three things forced that:

- **A destination would have varied the tool schema.** Whether `dm` can land is derived
  from the address, and tool definitions are a provider prompt-cache breakpoint sitting
  at the *front* of the cached prefix (`anthropic_cache_tool_definitions` /
  `bedrock_cache_tool_definitions`). Narrowing an enum per conversation forks that prefix
  into variants that never warm each other, and busts it outright the turn a conversation
  moves. The gate now keeps *all* runtime state out of the tool block — the route args are
  plain `str`, validated in the body — and refuses anything address-derived there, as
  `allow_here` does.
- **Landing in a DM main transfers ownership of it.** `Route` short-circuits on
  `thread.active_agent_tentacle_id` before anything else, so a handoff recorded on a DM
  re-points that user's private assistant for good. A `summon dm` would have let a group
  choose whose assistant someone gets.
- **So the receiver is not chosen at all.** `scheme(hint, brief)` hands the brief to
  whoever already owns that DM — or the channel default when nobody does, which then owns
  it. The model picks a *place*, never a person, and no group can rewire a private DM.

`open_dm` is a `ChannelTentacle` method: Slack calls `conversations.open` (needs the
`im:write` scope), Lark and NapCat address the user's own id, the dev UI returns `None`.
Capability now lives in `ChannelSurfaces` (`sub_thread`, `dm`) beside `thread_strategy`,
which is routing-only — the dev UI declares `flat_thread` and can open nothing.

Not carried over from the scoping below: history. `scheme` hands over a brief like any
handoff, so `fork`'s empty-target rule and DM sub-threads never enter it.

**Requirements** (as originally scoped)
- From a group, an agent can move the conversation into a **1:1 DM with the current user** and
  continue there — `summon dm` (hand off to another agent) and `teleport dm` (same agent, carry
  history). In a chat that is already a DM, `dm` is a no-op.
- Offered only where the channel can actually open a DM; refused otherwise (like the `here` gate).

**Prerequisites**
- **Open-DM primitive** — `ChannelTentacle.open_dm(user_id) -> ChannelAddress | None`. None
  exists today. Slack needs `conversations.open(users=[Uxxx]) → Dxxx`; Lark/NapCat can address a
  user directly (`chat_id` *is* the `open_id` / QQ id); web/`main_only` has no DM surface.
- **Idempotent-open reconciliation** — `conversations.open` returns the *existing* DM, which may
  already carry a derived owner + history. `ConversationManager.fork` fails fast on a non-empty
  target ([conversation.py:327-331](../../octomate/managers/conversation.py#L327)), so a
  `teleport` must land on an **empty** target (fresh DM, or a fresh thread inside the DM) or fall
  back to staying — never splice two histories. `summon` (brief, no fork) just appends a handoff.
- **Materializability filtering** — `scry`/candidate set must only offer `dm` when
  `open_dm` is available, so the agent can't pick an unopenable target.

## 2. Cross-channel / cross-platform continuation

**Requirements**
- An agent can continue on a **different `channel_tentacle_id`** (Slack→Lark, or another Slack
  workspace) — `summon`/`teleport` into that channel's DM with the same user.
- Only the **same user** (a linked identity), never a third party; the move is announced, not silent.

**Prerequisites**
- ~~**Everything in §1**~~ (open-DM primitive + reconciliation) — **done**; cross-channel
  materialization is §1 applied on the *target* channel, which `open_dm` now supports.
- ~~**Cross-platform identity registry**~~ — **done** ([user-identity.md](user-identity.md)):
  `users:` declares which channel profiles belong to one durable human, and `UserManager.owner`
  resolves a profile to that person. The remaining gap is the reverse lookup — *this user's
  profile on that other channel* — plus the two items below.
- **A way to name a remote target without leaking channel/user ids into tool args** (the
  send-toolset invariant) — offer reachable DMs as opaque, labeled handles the agent picks from,
  resolved to `(channel, user_id)` internally. **Not in the tool schema**: a per-user list of
  reachable DMs varies far more than the `dm` destination that §1 had to withdraw, and would
  fork the cached tool prefix per user rather than per address. Whatever names remote targets
  has to reach the model as a tool **result** — `scry`'s return value. Not a dynamic
  instruction: `anthropic_cache_instructions` / `bedrock_cache_instructions` are on
  (`config/providers.py:48-49,64-65`), so instructions are their own cached segment and a
  per-user one forks it just as a per-user schema forks the tool block. A tool result is
  the only carrier that sits after every breakpoint.
- **Consent / product policy** for opening an (unsolicited, possibly cross-platform) DM — a
  product call to settle before building.

## Notes

- Agents are not channel-bound and pydantic-ai history is channel-agnostic, so once a target DM
  can be opened, cross-channel `teleport` (fork) and `summon` (brief) reuse the existing
  `Handoff`/`Teleport` mechanism unchanged — the hard parts are the prerequisites above, not the
  dispatch.
- Cross-**runtime** agents (Claude/Codex) can only be `summon`ed (brief), never `teleport`ed —
  same rule as local teleport.
- `main_only` channels (NapCat, the dev UI) can't open a thread at all — which no longer matters
  for §1, since `scheme` hands over a brief and lands in the DM itself. It still bounds
  `summon thread` and `teleport`, now via `ChannelSurfaces.sub_thread` rather than
  `thread_strategy`.
