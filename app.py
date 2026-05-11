"""Octomate ASGI entry.

Loads `octomate.yaml`, builds + connects tentacles per config, exposes
`app` for uvicorn.

Run with:  `uv run uvicorn app:app --reload`
Or:        `uv run python app.py`
"""

from __future__ import annotations

from octomate import Octomate
from octomate.config import load_config
from octomate.tentacles.agent import InklingTentacle
from octomate.tentacles.channel.dev_ui.base import DevUITentacle

config = load_config()
octomate = Octomate()

octomate.connect(InklingTentacle("inkling", model_id=config.agents.inkling.model))
octomate.connect(DevUITentacle("dev_ui", agent_id=config.channels.dev_ui.agent_id))

app = octomate.app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
