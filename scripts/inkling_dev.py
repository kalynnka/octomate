"""Dev harness: serve the Inkling agent through the DevUI channel tentacle.

Run from the project root with:

    uv run uvicorn scripts.inkling_web:app --app-dir . --reload --port 8000

Then open:
- http://localhost:8000        — chat UI (pydantic-ai's bundled `@pydantic/ai-chat-ui`)
- http://localhost:8000/docs   — Swagger UI for /api/chat

`--app-dir .` is required: uvicorn doesn't add the cwd to `sys.path` by default,
and this project doesn't install `octomate` as an editable package.

Server-owned conversations:
- The DevUI tentacle ignores the `messages` array sent by the chat UI.
- Conversation history is loaded/persisted server-side via SessionStore +
  MessageStore (SQLite at .octomate/octomate.db by default; override with
  `OCTOMATE_DB_URL`). Run `uv run alembic upgrade head` once to create the
  schema.
- The Vercel SubmitMessage `id` is treated as the session id, so each chat
  thread persists across page reloads.
"""

from __future__ import annotations

from octomate.schemas.events import MessageEvent
from octomate.schemas.session import SessionKey
from octomate.tentacles.channel.dev_ui import DevUITentacle


class _NoopOctopus:
    """DevUI bypasses octopus.kick — the HTTP request is its own dispatch."""

    async def kick(
        self, key: SessionKey, contents: list[MessageEvent]
    ) -> None:
        return None


tentacle = DevUITentacle(id="dev_ui", octopus=_NoopOctopus())
app = tentacle.app
