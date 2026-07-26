# User identity registry

## Status

Implemented on this branch. OAuth connection work is planned separately in
[oauth-connections.md](oauth-connections.md).

## Decision

The `users:` section of `octomate.yaml` is the current authority that declares an active
Octomate user and says which channel profiles belong to that person. User rows are durable:
removing a declaration unlinks its profiles but does not delete the human's record. This leaves a
stable owner for future registration sources and user-owned data.

Channel access and registration are deliberately different:

- A person who can talk in an admitted channel/group may converse with Octomate.
- Their observed channel account is persisted as a `UserProfile`.
- If YAML does not declare that exact `(channel_tentacle_id, channel_user_id)`, the profile is an
  ownerless visitor.
- Only YAML reconciliation currently creates a `User` or attaches a profile to one.
- There is no runtime link code, claim/confirm flow, merge, birth user, or administrator mutation
  API.

This makes the central fact explicit: an observed channel account is evidence about a sender, not
by itself a declaration that a registered human exists.

## Domain model

### `User`

One durable human identity across channels, initially created by YAML:

```text
id          UUID
username    str, unique — stable `users:` YAML key
name        str — canonical display name, defaults to username
nickname    str | None
profiles    relation collection of UserProfile
```

The username is identity; `name` and `nickname` are mutable presentation. Renaming a person while
keeping the YAML key preserves the database user id and all user-owned data.

### `UserProfile`

One observed account in one configured channel:

```text
id                    UUID
channel_tentacle_id   str
channel_user_id       str
name                  str
nickname              str | None
gender                str | None
age                   int | None
title                 str | None
user_id               UUID | None
user                  Relation[User | None]
```

`(channel_tentacle_id, channel_user_id)` is unique. `user_id=None` is normal and means visitor.
The FK uses `ON DELETE SET NULL` if a user is ever explicitly deleted. Normal YAML reconciliation
retains user rows and nulls ownership for profiles no longer declared.

There is no link method or verification timestamp: with one authority, such fields would only
restate that the row came from YAML.

`UserProfile` remains both the common channel-boundary schema and the blessed persistence
transmuter. Boundary instances have an empty `channel_tentacle_id` and no owner. `UserManager`
creates or reloads the registry instance and stamps the channel id.

There is no legacy `user_id` input translation. YAML profiles use Arcanus's generated
`UserProfile.Create` schema, which excludes the server-generated identity and relationship and
validates writable fields with their real types. Unknown input fields are ignored consistently with
the shared channel profile boundary. A config boundary validator additionally requires
`channel_user_id` in mapping syntax. Internal profile construction also uses `channel_user_id`;
`user_id` means only the optional registered-owner FK. Platform adapters may map externally fixed
wire fields such as OneBot's `user_id` at their parsing boundary.

## Configuration

The YAML key is a stable username. `name` defaults to it:

```yaml
octomate:
  users:
    luhui:
      name: Lu Hui
      nickname: Lu
      profiles:
        slack:
          channel_user_id: U012345
        lark:
          channel_user_id: ou_abcdef
          name: Lu on Lark
```

Each channel entry is a complete mapping with an explicit `channel_user_id`; shorthand scalar ids
are rejected. Profile display fields seed a profile that has never been observed; once a channel has
supplied a snapshot, reconciliation never overwrites those channel-owned fields.

Configuration validation rejects profile channel ids that do not name a configured channel.
Reconciliation rejects one channel identity declared under two users before making database
changes.

## Reconciliation

`Octomate` runs `UserManager.reconcile()` before starting agent or channel tentacles.

Reconciliation makes persistence exactly match YAML ownership:

1. Validate the complete declaration for duplicate channel profiles.
2. Upsert users by stable username and update their name/nickname.
3. Load currently owned profiles.
4. Null ownership for profiles no longer declared.
5. Attach each declared profile to its configured user, creating an unseen profile when necessary.
6. Retain user rows whose usernames no longer exist in YAML for future registration sources and
   user-owned data.
7. Commit once and cache every persisted user.

The resulting transitions are:

| YAML change | Result |
|---|---|
| Add user | Create registered `User` |
| Rename user under same key | Update presentation, preserve id |
| Remove user | Retain `User`; profiles become visitors |
| Add existing visitor profile | Attach it without replacing observed display fields |
| Add unseen profile | Seed and attach it |
| Remove profile | Preserve row, set `user_id=None` |
| Move profile between users | Reassign ownership in the same reconciliation |

Users with no profiles are valid declarations, although they cannot be recognized from a channel
until YAML adds a profile.

## Observation path

`UserManager.ensure_profile(channel_tentacle_id, observed)` follows the normal manager pattern:

1. Query the indexed channel identity.
2. Create an ownerless profile on cache miss.
3. Otherwise replace the channel-owned display fields with the latest snapshot, including empty
   values.
4. Eagerly resolve the optional owner while the session is active.
5. Commit and return the persisted profile.

It never creates a `User` and never changes `user_id`. Concurrent first sightings are serialized so
the unique identity row is created once.

Profiles are not cached without bound. The user registry is expected to remain small, so every
persisted user is cached for repeated profile-to-owner resolution.

`UserManager.owner(profile)` returns the registered user or `None` for a visitor. Callers should not
manufacture a visitor `User` or interpret the profile display name as a canonical identity.

## Thread and prompt integration

Every thread message references the durable sender profile through `ThreadMessage.sender_id`.
`ThreadManager.record_inbound()` and `record_outbound()` call `ensure_profile()` before writing the
ledger. The inbound method replaces `event.sender` with the persisted registry instance.

Prompt rendering uses:

```text
Linked profile:  Lu (U012345, user:luhui)
Visitor profile: Alice (U098765)
```

The `user:<username>` marker is emitted only when `sender.user` exists. This gives all YAML-linked
profiles of one person the same cross-channel marker without pretending that visitors are known
users.

The Lark SDK's `user_id` field is explicitly excluded when creating a `LarkUserProfile`: Lark uses
that name for a platform id namespace, while the registry schema reserves `user_id` for the optional
owner FK. NapCat boundary validators similarly move OneBot's `user_id` into `channel_user_id`.

## Persistence migrations

Migration `f4a6f02876d3` introduces the final YAML-only schema directly: `users` with a stable
username, nullable `user_profiles.user_id` ownership, and durable sender-profile FKs for thread
messages. It backfills existing sender snapshots using `channel_user_id`; there is no intermediate
handle or runtime-link schema.

## OAuth boundary

OAuth connections are owned only by registered users, but they are not identity-linking mechanisms.
A future connection flow starts from the current persisted sender and proceeds only when
`sender.user_id` is set. It may neither attach a visitor profile nor accept a target user argument.

Removing one profile makes that channel account a visitor without transferring the user's OAuth
connections. Removing the YAML user retains the durable user and connections, but unlinks every
profile, so no sender can reach those connections. Re-adding the same stable username restores the
same user record. See [oauth-connections.md](oauth-connections.md) for the full owner-only flow.

## Acceptance

- A first message from an undeclared sender creates one ownerless profile and no user.
- Repeated observations refresh the profile and preserve its YAML ownership or visitor status.
- Concurrent first sightings create one profile.
- YAML is currently the only code path that creates users or changes profile ownership.
- Multiple declared profiles resolve to the same stable user.
- Duplicate declarations fail before partial reconciliation.
- User display-name changes are stable by username.
- Removed user declarations retain their user rows and leave their profiles as visitors.
- Thread history and live events show a cross-channel user marker only for registered senders.
- Focused user and thread-manager tests cover visitor, attach, move, remove, seed, refresh, and
  idempotent reconciliation behavior.
