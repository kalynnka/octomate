from __future__ import annotations

import uvicorn

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.tentacles.agent.inkling import InklingTentacle, build_inkling_agent
from octomate.tentacles.channel.slack import SlackTentacle
from octomate.web.dev_ui import build_dev_ui_router

config = OctomateConfig()
octomate = Octomate()
octomate.register_agent(
    "inkling",
    InklingTentacle(
        "inkling",
        agent=build_inkling_agent(config.agents.inkling.model),
        conversation_manager=octomate.conversations,
    ),
)

if (channel_config := config.channels.slack) is not None and channel_config.enabled:
    octomate.connect_channel(
        "slack",
        SlackTentacle(
            "slack",
            octomate,
            bot_token=channel_config.bot_token,
            app_token=channel_config.app_token,
            agent_id=channel_config.agent_id,
            mention_only=channel_config.mention_only,
        ),
    )


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
