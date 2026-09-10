# Thread history search

Octomate searches the visible chat ledger locally with SQLite FTS5 and BM25. The
ledger remains authoritative; the full-text table is a derived index that can be
rebuilt from `ThreadMessage.message_text` without losing chat history.

## Search contract

`search_thread_history` is discovery, not conversation replay. It returns a small
set of matching `ThreadMessage` rows and their handles. Use
`read_thread_history_before` or `read_thread_history_after` with one of those handles
to fetch the neighboring messages only when they are useful.

The first version supports English:

- SQLite's `porter unicode61` tokenizer folds case and stems related English word
  forms.
- Whitespace-delimited query terms are quoted and combined with `AND`; every term
  must match.
- FTS operators, quotes, and punctuation from a caller are treated as literal text,
  not executable query syntax.
- Results are ordered by ascending SQLite `bm25()` score, then by newest
  `happened_at` and message id when scores tie.
- A blank query is refused. There is no substring, multilingual, or semantic-search
  fallback.

SQLite's [FTS5 documentation](https://www.sqlite.org/fts5.html) defines the Porter
wrapper and notes that its BM25 implementation gives better matches numerically
lower scores.

## Visibility boundary

History is scoped to the person a run answers. A person can search every message in
each thread where one of their linked channel profiles has spoken. This includes
messages from other participants in a shared group thread, but excludes a different
person's private thread.

This is participation-based visibility, not channel membership. A silent group
member has no searchable access until they speak because Octomate does not yet
persist authoritative membership or revocation state.

The scope is enforced inside the ranked SQL statement, before `actor_kind`, ordering,
and `LIMIT` are applied. Never fetch global top matches and filter them in Python.

## Code path

1. [`ThreadManager.record_inbound()` and `record_outbound()`](../octomate/managers/thread.py)
   project visible text into `ThreadMessage.message_text`.
2. [`ThreadManager.store_message()`](../octomate/managers/thread.py) flushes the ledger
   row and writes its UUID and non-empty text to `thread_messages_fts` in the same
   transaction.
3. [`history_match_query()`](../octomate/managers/thread.py) turns plain English terms
   into a safely quoted FTS5 `AND` expression.
4. [`ThreadManager.search_chat_messages()`](../octomate/managers/thread.py) joins the
   FTS index to the ledger, applies the linked-profile scope and optional actor filter,
   ranks with BM25, and returns typed `ThreadMessage` objects.
5. [`HistoryCapability`](../octomate/capabilities/history.py) exposes the in-process
   tools. [`mount_history()`](../octomate/mcp/history.py) exposes the same contract to
   native runtimes over MCP.
6. The Alembic revision under [`octomate/migrations/versions`](../octomate/migrations/versions)
   creates and backfills the virtual table for existing databases.

Only `message_text` is indexed. Vendor payloads, segment JSON, model thinking, tool
traces, and the model ledger are outside the search corpus. Message body updates and
deletions are not currently supported; a future implementation of either must update
the derived index in the same transaction.

Repository engineering, persistence, typing, and verification rules remain
canonical in [`AGENTS.md`](../AGENTS.md). Do not copy them into feature code or a
second style guide that can drift.

## Automated verification

From the repository root:

```bash
uv run pytest -q \
  tests/agent/test_history.py \
  tests/agent/test_thread_manager.py \
  tests/agent/test_gateway_tools.py \
  tests/cli/test_deployment.py
uv run pytest -q
```

Run Ruff formatting and checks with explicit changed paths, then run Pyright in the
repository's configured basic mode. The deployment tests create a real migrated
SQLite database, verify FTS5, backfill an existing message, and prove downgrade drops
only the derived index.

## Manual WebUI verification

Back up the configured SQLite database and stop the backend before applying a pending
migration. For a disposable development database, migrate and start the two services:

```bash
uv run alembic -c octomate/migrations/alembic.ini upgrade head
uv run octomate service serve --reload
```

In another terminal:

```bash
cd trunkline
pnpm dev
```

Open `http://localhost:5173`, then:

1. In one task, send several messages including a concise distinctive sentence such
   as `We corrected the authentication setting.` Put an unrelated message immediately
   before and after it.
2. Open a second task as the same user. Ask the agent to call history search for
   `correcting settings` and show the search result before reading more context.
3. Inspect the history tool card. The stemmed query should find the earlier sentence
   even though the word forms differ, and the result should expose a row id or
   `#msg:<id>` handle.
4. Ask the agent to call the history read-after tool with that handle. The unrelated
   neighboring message should appear only in this paging step.
5. Repeat with two required terms where only one appears in another message. That
   partial match must not be returned.

Authorization and migration edge cases are deterministic in automated tests; the
WebUI pass verifies that the configured model receives the tools and follows the
search-then-page workflow end to end.
