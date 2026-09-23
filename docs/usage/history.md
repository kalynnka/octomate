# History

An agent answering you can read every thread you have spoken in: this one, your
direct messages elsewhere, the group a hand-off came from, a terminal session you
ran last week, on any of your linked accounts. Messages that did not wake the agent
are there too. The scope is the person, not the thread.

## Tools

| Inkling | Over MCP | Purpose |
|---|---|---|
| `search_thread_history` | `history_search` | Find messages matching every term, best match first |
| `read_thread_history_before` | `history_read_before` | The messages just before a given one, oldest first |
| `read_thread_history_after` | `history_read_after` | The messages just after |

A search takes a query, an optional actor kind (`human`, `agent`, `bot` or
`system`) and a limit, ten by default. The paging tools take a message's row id or
its `#msg:<id>` handle, as a hit or a brief shows it. Over MCP each line is clipped
at 400 characters and a page over 50 is refused rather than clamped, since those
runtimes have no spill store to catch an oversized return.

Search is discovery, not replay: it returns a handful of hits, and paging around one
is how the agent reads the context it needs.

## What is searchable

Only the visible text of the chat ledger: what people, bots and agents said, taken
from text and markdown segments. Tool arguments, thinking, attachments and the
model transcripts are outside the index. The search is English, lexical:

- Porter stemming folds related word forms, so "correcting settings" finds "We
  corrected the authentication setting".
- Every whitespace-separated term must match. Operators and quotes in a query are
  literal text.
- Ranking is BM25, ties broken newest first.
- No substring match, no other languages, no semantic search.

## Visibility

A thread is readable because one of the asker's linked profiles has spoken in it.
That includes everything other people said in a shared thread, and excludes a
thread the person never spoke in, however much they could see it on the platform.
Octomate does not track channel membership. A visitor, an unlinked profile, has one
account's worth of history and can be followed nowhere.

The scope is applied inside the search itself, before ranking and limits, so a
result never leaks and gets filtered afterwards.

## Under the hood

The index is a SQLite FTS5 table maintained by triggers on the ledger, in the
ledger's own transaction, and rebuilt from the ledger rows if dropped. Migrations
own it. See [Persistence](../concepts/persistence.md).
