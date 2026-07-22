# Plan (rough): DM + cross-channel continuation

> **Status:** draft (requirements + prerequisites only) · **Owner:** @luhui · **Created:** 2026-07-06
> · **Updated:** 2026-07-22 (drift fixes after [subagent-runs](done/subagent-runs.md))
> **Builds on:** [done/self-routing-dispatch.md](done/self-routing-dispatch.md) — unparks its
> two deferred `gate` destinations. **Reference:** [cancelled/channel-retargeting.md](cancelled/channel-retargeting.md) §0b.

The shipped `gate` (`GatewayCapability`, [gateway.py](../../octomate/capabilities/gateway.py))
routes a turn only to **local** surfaces — `here` (current thread) and `thread` (a sub-thread of
the current chat) — because `summon`/`teleport` open every surface through `start_sub_thread`,
which stays on the same channel + `chat_id`. The gate has since gained `scheme`/`whisper`
(subagent spells); they are orthogonal here — a new destination touches only `summon`/`teleport`.
Two destinations were parked. This is a rough scoping of what each **requires** and what must
exist **first**.

## 1. `dm` destination — continue 1:1 in the user's DM

**Requirements**
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
- **Everything in §1** (open-DM primitive + reconciliation) — cross-channel materialization is
  §1 applied on the *target* channel.
- **Cross-platform identity registry** — none exists; `user_id` is single-platform. Need an
  **explicit** person↔`(channel, user_id)` link model (opt-in `/link` handshake). **No implicit
  matching** on name/email (privacy + false-merge hazard). Without a link, the destination is
  simply unavailable. Scoped as its own plan: [user-identity.md](user-identity.md).
- **A way to name a remote target without leaking channel/user ids into tool args** (the
  send-toolset invariant) — offer reachable DMs as opaque, labeled handles the agent picks from,
  resolved to `(channel, user_id)` internally. The gate's `narrowed(...)` runtime-`Literal`
  mechanism ([gateway.py:125-141](../../octomate/capabilities/gateway.py#L125)) is the ready-made
  way to offer them — the same trick that narrows `agent_id`/`model` today.
- **Consent / product policy** for opening an (unsolicited, possibly cross-platform) DM — a
  product call to settle before building.

## Notes

- Agents are not channel-bound and pydantic-ai history is channel-agnostic, so once a target DM
  can be opened, cross-channel `teleport` (fork) and `summon` (brief) reuse the existing
  `Handoff`/`Teleport` mechanism unchanged — the hard parts are the prerequisites above, not the
  dispatch.
- Cross-**runtime** agents (Claude/Codex) can only be `summon`ed (brief), never `teleport`ed —
  same rule as local teleport.
- `main_only` channels (NapCat, web) can't thread a DM, so `teleport dm` there is empty-or-nothing.
