from __future__ import annotations

import uvicorn

from octomate import Octomate
from octomate.tentacles.agent.inkling import build_inkling_agent
from octomate.web.dev_ui import build_dev_ui_router

octomate = Octomate()
octomate.register_agent("inkling", build_inkling_agent())
octomate.include_router(
    build_dev_ui_router(
        octomate,
        channel_id="dev_ui",
        agent_id="inkling",
    )
)

app = octomate.app(title="Octomate")


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
