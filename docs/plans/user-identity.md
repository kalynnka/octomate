# Plan: cross-channel user identity

> **Status:** in progress — UoW-1/2 built, UoW-3 absorbed into UoW-1 (2026-07-22, uncommitted); UoW-4/5 pending · **Owner:** @luhui
> · **Created:** 2026-07-22
> **Feeds:** [dm-and-cross-channel-continuation.md](dm-and-cross-channel-continuation.md) §2 —
> its "cross-platform identity registry" prerequisite, scoped here as its own unit.

`user_id` is a bare per-channel string (Slack `Uxxx`, Lark `open_id`, QQ number, web
`DEV_USER_ID`). Nothing anywhere records that two of them are the same human, so neither the
system nor the agent can recognize one person behind two nicknames. This plan owns exactly that:
a `User` entity, verified per-channel profiles hanging off it, and surfacing the identity to the
system and the agent. Dispatch (`dm` destination, cross-channel `summon`/`teleport`) stays in
the parent plan. Of the link candidates below, **A (config) and B (link-code) land here**; C–E
stay recorded as candidates.

## Current state (2026-07-22)

- `user_id` is persisted only on `ThreadMessage.user_id`
  ([thread.py:194](../../octomate/models/thread.py#L194)) and `DeferredAction.responder_id`
  ([deferred.py:130](../../octomate/models/deferred.py#L130)) — raw platform strings, one
  namespace per channel tentacle.
- `UserProfile` ([conversation.py:136](../../octomate/schemas/conversation.py#L136)) is an
  ephemeral per-channel snapshot (name/nickname/…), cached in memory on the channel tentacle,
  persisted only as a JSON blob on each `ThreadMessage.sender` — never a row of its own.
- `ChannelAddress.user_memory_key` (`user:{channel}:{user_id}`,
  [conversation.py:48-50](../../octomate/schemas/conversation.py#L48)) is defined but referenced
  nowhere — an aspirational per-channel key that this registry would subsume.
- No person/identity/link/alias table exists (table inventory: threads, thread_messages,
  message_binding, channel_handoffs, conversations, agent_runs, deferred_action_batches,
  deferred_actions, model_messages, todos).

## Three sub-problems

1. **Registry** — the data model: a user plus verified per-channel profiles, unique per
   `(channel, channel_user_id)`. Every candidate below needs this; they differ only in how a
   link is *created*.
2. **Proof** — what evidence establishes "these two accounts are one human". Security-critical;
   the parent plan already rules out implicit name/email *merging* (false-merge hazard, privacy).
3. **Recognition** — once links exist, surface them: ingest/prompt render the identity (the
   agent sees one person behind two nicknames), and user-keyed memory/preferences become
   possible.

## Candidates for creating links (the proof)

- **A. Config-declared (operator bootstrap).** ✦ **lands here (UoW-2).** Declare users and
  their platform ids in `octomate.yaml`; the operator is the trust root. No protocol, no wizard
  — mirrors how route claims went config-first. Weakness: static, operator-only, requires
  digging up raw platform ids.
- **B. Link-code handshake.** ✦ **lands here (UoW-5).** Ask to link on channel A → short-lived
  one-time code → present it from the other account on channel B. Possession across both
  accounts is the proof. Plain-text transport, so it works everywhere including NapCat. Needs
  code issuance/TTL/single-use state, and a direction policy (issue on the already-trusted
  side, redeem on the new side, so a stranger can't harvest a code out of the trusted account).
- **C. Claim-then-confirm on a trusted channel.** *Deferred* until the parent plan's §1 open-DM
  primitive exists. A new `(channel, channel_user_id)` claims to be an existing user → an
  approval card goes to that user on an already-linked channel → one tap. Reuses the
  deferred-action approval feelers; inherently impersonation-safe because the existing identity
  holds the approve button. The `method` column simply gains a `"confirm"` value when this
  lands.
- **D. Web identity root (magic link / SSO).** *Non-goal.* A web login becomes the canonical
  user; channel cards carry a signed URL button. Best UX on Slack/Lark, generalizes to per-user
  OAuth (parked in [done/github-linear-mcp.md](done/github-linear-mcp.md)), but it is real auth
  infrastructure and the web channel transport is still a stub.
- **E. Implicit evidence, explicit confirmation.** *Deferred.* Verified platform emails
  (Slack/Lark expose them; QQ doesn't) generate a *suggestion* only — confirmation still flows
  through B or C. A friction-reducer on top of B/C, not a standalone mechanism.
- ~~F. Shared passphrase per user~~ — rejected: secrets land in third-party chat logs and are
  replayable forever.

Wizard policy for B: **just-in-time, not startup.** The agent offers linking when it unlocks
something — the user asks, or a cross-channel need appears. No first-contact wizard: it
interrogates group bystanders, and a "pick which existing user you are" card enumerates the
registry to strangers.

## Decisions (2026-07-22): data structure & manager

- **Naming: `User` + `UserProfile`** (owner call, settled in review 2026-07-22). The persisted
  entity is `user_profiles` — each row is one user's identity in one channel — and "link" is
  not a table or entity at all: it is the nullable **`user_id`** FK (`User.profiles` /
  `UserProfile.user` is the relationship). The platform string is **`channel_user_id`**,
  pairing with `channel_tentacle_id`. ⚠ This naming has a real cost the owner accepted: on the
  wire, `user_id` historically means the platform string (every platform payload, every
  pre-registry `ThreadMessage.sender` blob), pydantic always matches a field by its *name*
  (`validate_by_name`), and arcanus reads a field's *alias* as the provider column name — so no
  alias arrangement can separate the two meanings. The guard is a `mode="before"` validator on
  `UserProfile` (`claim_legacy_user_id`): a dict without `channel_user_id` is the legacy shape,
  and its `user_id` key is claimed as the platform string, never the FK. The NapCat normalizers
  additionally pop `user_id` into `channel_user_id` themselves, so base-vs-subclass validator
  ordering is never load-bearing. ⚠ Residual hazard to watch in review and future channels: a
  missed `.user_id` *read* on a profile now silently returns the FK (`None`) instead of the
  platform string — every profile read must say `channel_user_id`.
- **Promote, don't duplicate.** The existing `UserProfile` is **promoted to the transmuter
  itself** ([schemas/user.py](../../octomate/schemas/user.py)) — no second class: still parsed
  ephemerally at the boundary as today, persisted when the registry cares. The entity fields
  (`id`, `channel_tentacle_id`, `user_id`, `method`, `verified_at`) all default, so ephemeral
  boundary instances stay valid; the before-validator guards legacy platform payload dicts.
- **The ledger references the registry** (owner call, 2026-07-22). `ThreadMessage.sender` is no
  longer a JSON snapshot: every row carries `sender_id` → the sender's `user_profiles` row
  (inbound: the platform account; outbound: the channel bot or a native session's pseudo-user),
  with `sender` an arcanus `Relation`. Consequences: the `__clause_element__` workaround was
  deleted outright (no transmuter instance rides a JSON column anymore); `ensure_profile` moved
  forward from UoW-3 into `ThreadManager` — every ledger write ensures the sender's registry
  row, covering channel and native paths alike; `record_outbound` requires an honest `sender`
  (never fabricated — the channel bot's profile or `NATIVE_USER`); and sender display resolves
  the *live* profile, not a message-time snapshot. The migration backfills historical blobs
  (latest snapshot wins; historical outbound fabrications become unlinked observation rows).
- **Explicit links, observed profiles.** A `user_profiles` row is an *observation*: it
  materializes on first sight of any `(channel, channel_user_id)` — safe to auto-create,
  since a profile is a fact about a channel, not an identity claim. The *link* — setting its
  `user_id` FK — happens only via config (A) or a completed handshake (B). Unlink NULLs the FK
  and keeps the row. `User` rows are never auto-provisioned, so no merge machinery ever.
- **Users come from config only (this plan's scope).** B links a new channel account to an
  *existing* user; it never creates one. `handle` is always the config key — no
  generated-handle policy needed until some future runtime-creation flow.

### Sketch

```
users
  id      UUID pk (uuid7)
  handle  str, unique     — stable slug; the config key
  name    str             — canonical display name the agent uses for this human

user_profiles  — one row per (channel, platform user) ever seen: a user's identity in a channel
  id                   UUID pk
  channel_tentacle_id  str
  channel_user_id      str  — platform id (Slack Uxxx, Lark open_id, QQ number)
  UNIQUE (channel_tentacle_id, channel_user_id)
  # the profile shape as columns, refreshed on ingest
  name, nickname, gender, age, title
  # the link — just this relationship, set only when proven
  user_id              UUID | None, FK users.id ON DELETE SET NULL — None until linked
  method               Literal["config", "code"] | None — set iff user_id is; C adds "confirm"
  verified_at          datetime | None — set iff user_id is
```

A nullable FK is ordinary SQL and already has repo precedent:
`Conversation.parent_conversation_id`
([conversation.py:62-74](../../octomate/models/conversation.py#L62)) is `Mapped[uuid.UUID | None]`
with `ForeignKey(..., ondelete="SET NULL")` — the constraint only validates non-NULL values. The
`user_id`/`method`/`verified_at` all-or-none pairing is enforced in the manager, the same way
`ConversationManager.ensure` validates the `subagent_id`/`parent_conversation_id` pairing.

Config (solution A), keyed by `handle`, channel keys matching the ids under `channels:` —
each value is a `UserProfile` (the schema reused as config), with a bare platform id as
shorthand; profile fields seed a never-seen account and never overwrite observations:

```yaml
users:
  luhui:
    name: Lu Hui
    profiles:
      slack: U0123ABCD
      napcat: "123456789"
      lark:
        channel_user_id: ou_xxxx
        name: Lu on Lark
```

## UoW-1 — the registry ✅ built (2026-07-22, uncommitted)

- **Promoted `UserProfile`** → [schemas/user.py](../../octomate/schemas/user.py), beside the new
  `User` transmuter:
  - **The platform field renamed to `channel_user_id`; the FK took `user_id`** (see
    Decisions ⚠, settled in review). The `claim_legacy_user_id` before-validator guards the
    legacy wire shape; the NapCat normalizers pop `user_id` → `channel_user_id` at their own
    boundary; every first-party construction and read site was renamed (a missed read returns
    the FK's `None` silently — grep `\.user_id` when touching profile code). Import paths also
    moved (`schemas/conversation.py` → `schemas/user.py` across schemas, channels, managers,
    tests).
  - Config: dropped `frozen=True`, added `from_attributes=True`; kept `validate_by_name`,
    `validate_by_alias`, `coerce_numbers_to_str`, `extra="ignore"` — the boundary-parsing
    features are now features of the entity.
  - New fields all default (`id` uuid7, `channel_tentacle_id=""`, link fields `None`) so
    ephemeral uses — `MessageEvent.sender`, the tentacle's own `profile`, old sender blobs —
    stay valid *unpersisted* instances. The manager stamps `channel_tentacle_id` at persist.
  - The channel profile subclasses (`SlackUserProfile`, `LarkUserProfile`,
    `NapcatUserProfile`, `NapcatSender`) stay unblessed transmuter subclasses used purely as
    parsers; their `user_id` redeclarations renamed to `channel_user_id` (Lark keeps its
    `open_id` validation alias). Their field redeclarations now trip pydantic's
    shadows-parent warning (the metaclass serves same-named column accessors); filtered in
    `octomate/__init__.py` + mirrored in pyproject's pytest `filterwarnings`.
  - ⚠ **Frozen is gone.** Boundary instances are cached and shared
    (`ChannelTentacle.user_profiles`); nothing may mutate them — only the manager's registry
    copies are session-bound and mutated. Convention, not enforcement; said in the docstring.
  - ⚠ **Discovered: `bless()` injects `__clause_element__`** on the class, and SQLAlchemy
    probes bind *values* for that attribute — a profile instance riding inside
    `ThreadMessage.sender`'s then-JSON column was mistaken for a SQL element at flush.
    Resolved by normalizing the ledger (`sender_id` FK, see Decisions), which removed the
    interim class-only descriptor. The trap remains live upstream: storing any transmuter
    instance as a JSON column value re-triggers it — an arcanus fix (inject class-only) is
    still worthwhile.
  - ⚠ **Discovered: on a blessed transmuter, `Field(alias=...)` names the provider column**
    (and the ORM-twin kwarg), not a wire alias. Never put wire aliases on transmuter fields;
    use `validation_alias` if one is ever needed.
- **Transmuter `User`**: `id`/`handle`/`name`,
  `profiles: RelationCollection[UserProfile] = Relationships()`. A user's linked profiles come
  off this relationship — what the parent plan's DM-destination materialization will consume;
  no separate accessor.
- **ORM** ([models/user.py](../../octomate/models/user.py)): `users` + `user_profiles` per the
  sketch; unique `(channel_tentacle_id, channel_user_id)`; `user_id` FK `ondelete="SET NULL"`,
  indexed.
- **Manager** ([managers/user.py](../../octomate/managers/user.py)): `UserManager`, wired as an
  `Octomate` field beside the other managers; `main.py` passes `config.users` at construction.
  - Whole-registry cache — `dict[(channel_tentacle_id, channel_user_id), UserProfile]` plus
    `dict[handle, User]` — `load()`ed in the app lifespan before tentacles start, kept coherent
    on writes. Unlike `ConversationManager`'s LRU: `resolve` sits on the ingest hot path and
    the registry is small.
  - `resolve(channel_tentacle_id, user_id) -> UserProfile | None` — pure cache lookup;
    `owner_of(profile)` resolves the linked `User`, `None` FK means unlinked.
  - `link(user, channel_tentacle_id, user_id, *, method)` / `unlink(...)` — set/NULL the FK
    (+ `method`/`verified_at` together, all-or-none) on the existing profile row, creating a
    bare row for an account never yet seen. **Fail fast if the pair is already linked to a
    different user** — no silent re-link (false-merge hazard).
- **Migration** `a91c04d5e7f2`: two `op.create_table`; verified upgrade → downgrade → upgrade
  against the live schema lineage. ⚠ SQLite FK enforcement is off in this repo —
  `ON DELETE SET NULL` is inert; the manager NULLs a deleted user's profile FKs explicitly.

**Acceptance (verified):** promoted `UserProfile` round-trips 50 real legacy sender blobs and
the platform payload shapes; tables exist; `link`/`unlink` enforce the pairing and cross-user
fail-fast; full suite green (545 passed) with no behavior change elsewhere.

## UoW-2 — config users (solution A) ✅ built (2026-07-22, uncommitted)

- **Config** ([config/users.py](../../octomate/config/users.py)): `UserConfig` (`name`,
  `profiles: dict[str, UserProfile]` — channel id → the user's profile there, bare platform id
  as shorthand), `users: dict[str, UserConfig]` on the root settings; the key is the `handle`.
  An unknown channel key fails at **config parse time** (a `model_validator` on
  `OctomateConfig`, mirroring the channel-route validator) — even earlier than reconcile.
- **`UserManager.reconcile()`**, called in the app lifespan after `load()`, before tentacles
  start. Config is the authority for everything it declares:
  - Upsert `User` rows by `handle` (name updates in place); delete users removed from config —
    their profiles' FKs are NULLed (including `method="code"` links: no user, no link).
  - For each declared link: ensure the profile row (bare if never seen), set
    FK/`method="config"`/`verified_at`. Drop config links absent from config (NULL the FK).
  - ⚠ Never touch `method="code"` links of a *surviving* user — reconcile owns only what config
    declares.
- Idempotent: a second boot with the same config is a no-op (no duplicate rows, no rewrites).

**Acceptance:** declaring a user with slack + napcat ids yields linked profiles after boot;
removing a link (or the whole user) from config unlinks on next boot; a handshake link on a
surviving user survives reconcile; restart is idempotent; unknown channel key fails boot loudly.

## UoW-3 — observed profiles on ingest ✅ absorbed into UoW-1 (2026-07-22)

Superseded by the ledger-references-registry decision: `ThreadManager.record_inbound` /
`record_outbound` call `UserManager.ensure_profile` on **every ledger write** (create on first
sight, refresh only when a profile field actually changed), which covers channel and native
paths alike — an ingest-site hook would be redundant. The per-tentacle `user_profiles`
in-memory cache stays as the platform-fetch cache; the registry is persistence, not fetch
avoidance.

## UoW-4 — recognition rendering

- At ingest, after `ensure_profile`, resolve the link: if the sender's registry row is linked,
  stamp the identity on the event — `MessageEvent` gains `sender_handle: str = ""` (empty =
  unlinked), set from the linked user's `handle`.
- Render it in the sender line ([events.py:45-48](../../octomate/schemas/events.py#L45)) —
  both `prompt_line` and the header variant. Shape at impl discretion, but it must keep the
  raw platform id and add a stable cross-channel marker, e.g.
  `Lu Hui (U0123ABCD, user:luhui)` — the `user:{handle}` token is what lets the agent equate
  senders across channels and transcripts.
- Unlinked senders render exactly as today.

**Acceptance:** the same human messaging from Slack and QQ produces the same `user:{handle}`
token in both conversations' prompts; unlinked senders' prompt lines are byte-identical to
before; the agent can be asked "who is user:luhui here?" and answer from the transcript alone.

## UoW-5 — link-code handshake (solution B)

- **Code store — in-memory on `UserManager`**, deliberately not a table: restart voids pending
  codes, which is acceptable for a 10-minute artifact.
  - `dict[code, PendingLink]` where `PendingLink` carries the issuing user id, the issuing
    `(channel_tentacle_id, channel_user_id)`, and `issued_at`. TTL ~10 min, checked at
    redeem;
    expired entries purged lazily. Single-use: popped on redeem. One outstanding code per user
    — re-issue replaces. Codes from `secrets.token_urlsafe`-class generation, short enough to
    retype across devices.
  - `issue_link_code(channel_tentacle_id, channel_user_id) -> str` — **requires the issuing
    sender to resolve to a linked user** (the trust-anchor direction: issue on the trusted
    side, redeem on the new side).
  - `redeem_link_code(code, channel_tentacle_id, channel_user_id)` — validates
    TTL/single-use/known code; `link(..., method="code")`, inheriting its cross-user
    fail-fast; refuses redeeming from the issuing account itself (a no-op claim).
- **Capability** (`capabilities/identity.py`): a small `AbstractCapability` in the
  `SendCapability` shape, holding the `UserManager` and the current sender's `ChannelAddress`.
  Tools take **no channel/user ids** — the send-toolset invariant; they act on the current
  sender only:
  - `link_code()` — issue for the current sender; the agent relays the code and tells the user
    to present it from their other account.
  - `link_redeem(code)` — redeem for the current sender.
  - `unlink()` — unlink the current sender's account if `method="code"`; refuse
    `method="config"` (config is authoritative — edit `octomate.yaml` instead).
  - Instructions: offer linking just-in-time (the user asks, or a cross-channel need appears);
    never solicit bystanders in group chats; prefer running both legs in private chats.
- **Wiring**: constructed per-React-run in `reflex/graph.py` beside the gate
  ([graph.py:513-524](../../octomate/reflex/graph.py#L513)), where the target address is known.
  Subagent runs don't get it (same policy as the gate: a non-interactive accomplice has no
  business linking identities).
- ⚠ **Group-visibility hazard**: a code typed in a group is readable by bystanders within its
  TTL. Mitigations here: single-use + short TTL + the prefer-private-chats instruction + the
  redeeming-side announcement naming which user got linked. Proactively notifying the *linked
  user on the trusted channel* needs the parent plan's open-DM primitive — until then this is
  the honest residual risk, and `unlink` is the recovery.

**Acceptance:** full round trip in tests — issue from a linked Slack sender, redeem from a
fresh NapCat sender → profile linked with `method="code"`; expired, reused, and unknown codes
fail with clear `ModelRetry` messages; redeem from an account already linked to a different
user fails fast; `unlink` removes a code link and refuses a config link.

## Non-goals

- Candidates **C** (needs open-DM), **D**, **E** — recorded above, not built here.
- Runtime `User` creation — every user comes from config; B only adds accounts to existing
  users.
- User-keyed memory/preferences (`user_memory_key` → `user:{handle}`) — future work that this
  registry enables; coordinate with memory plans when they exist.
- Startup/onboarding wizard of any kind.
- Cross-channel dispatch itself — the parent plan consumes `User.profiles`; nothing here opens
  a DM or moves a conversation.
