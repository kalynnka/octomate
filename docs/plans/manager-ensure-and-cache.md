# Plan (raw): what `ensure` + an LRU cache is standing in for

> **Status:** raw note, nothing designed yet — captured to be discussed ·
> **Owner:** @luhui · **Created:** 2026-07-29 · **Updated:** 2026-07-29
> **Trigger:** a `threads` UNIQUE violation killed a live tailer loop; found while fixing
> `test_recover_overlapping_a_live_follow_leaves_it_tailing`.

`ThreadManager` and `ConversationManager` both resolve a durable row by identity, cache it in a
count-bounded `OrderedDict`, and create it on a miss. The pattern is hand-rolled in each manager
rather than shared, and the two copies have already drifted apart in a way that costs turns.

## The concrete bug

[`ConversationManager.__init__`](../../octomate/managers/conversation.py) holds an `ensure_lock`,
and its comment names the exact hazard:

> Serializes first sightings: two concurrent ensures of one identity — a session's follow task
> preparing while a hook pokes it, a commission fan-out landing twice in one thread — must not both
> insert. Under the lock the loser re-checks the cache and becomes a hit instead of a UNIQUE
> violation.

[`ThreadManager.ensure`](../../octomate/managers/thread.py) has no such lock. It reads, finds
nothing, and `session.add`s — so two coroutines that miss together both insert, and the loser gets:

```
sqlite3.IntegrityError: UNIQUE constraint failed:
  threads.channel_tentacle_id, threads.chat_type, threads.chat_id, threads.thread_id
```

The scenario the conversation lock was written for — *a session's follow task preparing while a
hook pokes it* — is precisely what hits the thread table. There the error escapes into `follow`'s
blanket handler, which stops tailing and, in the words of the test that guards the neighbouring
case, "silently strands every later turn of the session". It is reachable from ordinary traffic
too: two people replying at once in a chat with no thread row yet.

Adding the same lock to `ThreadManager` would close it in a line. Worth doing on its own — but the
duplication is the reason it was missable, which is the part to actually talk about.

## Why the shape deserves a revisit

**The invariant lives in prose.** That two `ensure`s of one identity must not both insert is a
property of every manager doing this, enforced in one of them by a lock a reader has to notice.
Nothing makes the next manager inherit it.

**Count-bounded eviction can hand out a second object for one row.** Both caches evict the
least-recently-used entry past 256. A caller still holding the evicted `Thread` keeps mutating an
object the manager will no longer return; the next `ensure` reads the row again and produces a
different instance for the same identity. Two live objects, one row, and whichever commits last
wins. The bound is on entries rather than on anything the row costs, so the eviction point is
arbitrary with respect to the risk.

**It is an identity map, rebuilt.** SQLAlchemy already keeps a session-scoped identity map, and
Arcanus sits on top of it. These caches exist because the session is per-operation while the
objects want to outlive it — so the managers reimplement identity at process scope, including the
locking and eviction that come with it.

## Directions, undecided

- Give `ThreadManager` the lock and stop there — smallest, leaves the duplication.
- Factor one `ensure` primitive (cache, lock, evict) both managers use, so the invariant is
  structural rather than remembered.
- Push creation into the database: upsert / `ON CONFLICT DO NOTHING` then re-read, making a
  concurrent first sighting a normal outcome rather than a race to lose.
- Per-key locks instead of one manager-wide lock, if the single lock becomes a contention point.
- Reconsider whether a process-scope cache is wanted at all, or whether the session's own identity
  map plus a narrower hot-path cache would do — and if it stays, whether cached objects should be
  immutable snapshots so an evicted reference cannot drift.

## Open questions

- Which rows genuinely need identity across sessions, and which are read-mostly?
- What is the real cost of a miss — is the cache load-bearing for latency, or historical?
- Does anything today depend on getting the *same* object back from two `ensure`s, or only on the
  same data? The answer decides whether snapshots are viable.
