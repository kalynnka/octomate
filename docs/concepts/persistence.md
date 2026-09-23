# Persistence

Octomate keeps everything in one SQLite database through
[Arcanus](https://kalynnka.github.io/arcanus/), which binds Pydantic schemas to
SQLAlchemy rows so that the validated object and the persisted row are the same
thing.

## Two layers

- **Models**, in `octomate/models/`, are the SQLAlchemy declarative classes. They
  own columns, indexes, constraints, and each column's documentation as a
  `comment`.
- **Schemas**, in `octomate/schemas/`, are Arcanus transmuters blessed onto those
  models. Managers and everything above them use schemas only. A loaded schema
  object is mutated and committed; nobody issues manual updates, and nobody imports
  a model except to define the mapping.

Foreign keys are enforced on every connection, in tests too, so a row needs its
parents to exist. The engine sets four SQLite pragmas per connection: write-ahead
logging, normal synchronisation, a five-second busy timeout, and foreign keys on.

## The domain

```text
Thread                           a chat room, or a piece of work
 ├─ parent_thread_id             one level: a sub-thread of a room
 ├─ project_id                   only for a thread or a native thread
 ├─ ThreadMessage                the chat ledger, one row per visible message
 │    └─ MessageBinding ──▶ ModelMessage
 ├─ Handoff                      append-only ownership transfers
 └─ Conversation                 one per (agent, subagent) in the thread
      └─ AgentRun                one turn, driven or replayed from a transcript
           └─ ModelMessage       the transcript
```

Around it: users and their channel profiles, invitations, sessions and API keys;
deferred action batches and their actions; installed MCP servers and OAuth
operations and connections; todos per conversation; projects; spilled tool
outputs. Runs are polymorphic on `kind`, so a native turn replayed from a
transcript carries its byte range and last line alongside a driven run's fields.

### Rooms, threads and the ledger

A room and a task thread share the `Thread` model. A room message can open a
sub-thread with recent room messages as a recap; the agent's context belongs to
that sub-thread. An active agent is pinned by a handoff, with append-only records
of who took over and why. Group room roots remain unowned so one exchange cannot
capture all future group messages.

The chat ledger holds visible messages. Agent conversations hold their separate
model context and run transcripts. History search and handoff briefs use the
ledger rather than copying another agent's private transcript. Message handles
such as `#msg:<platform id>` let tools page around a search hit or cite a message.
History visibility comes from participation by the user's linked profiles, not
from platform group membership.

## Migrations

Every schema change is an Alembic revision produced by
`uv run alembic revision --autogenerate -m "..."` against a copy of a database,
then adjusted: a docstring saying why, a data backfill if needed, an inferred
operation dropped if unwanted. The schema operations are never typed by hand, and a
column's comment reaches the migration by being generated from the model. If
autogenerate produces nothing, the model change is missing.

The migration environment resolves the database exactly as the server does, from
`OCTOMATE_DB_URL`, then `db_url` in the config home, then the default. It runs in
batch mode for SQLite's limited `ALTER`, and it excludes the full-text search
tables from comparison, since those are owned by a migration's own SQL.

## History search

The chat ledger's text is indexed in an FTS5 virtual table with the Porter
tokenizer, maintained by insert, update and delete triggers in the ledger's own
transaction. Only `message_text` is indexed. The index is derived: dropping and
rebuilding it loses no history. Trigger changes are authored in revisions by hand,
because autogenerate does not see them.

## Spills

An oversized tool return is stored whole in the database, compressed, under a
handle minted by the tool-output capability, with a retention window after which
it is pruned. The database rather than a file because a conversation is answered
from whichever process picks it up.
