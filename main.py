from __future__ import annotations

import logging

import uvicorn

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.tentacles.agent.inkling import InklingTentacle, build_inkling_agent
from octomate.tentacles.channel.lark import LarkTentacle
from octomate.tentacles.channel.napcat import NapcatTentacle
from octomate.tentacles.channel.slack import SlackTentacle
from octomate.web.dev_ui import build_dev_ui_router

config = OctomateConfig()
logging.basicConfig(
    level=config.logging.level,
    format=config.logging.format,
    force=True,
)
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
            config=channel_config,
        ),
    )

if (channel_config := config.channels.lark) is not None and channel_config.enabled:
    octomate.connect_channel(
        "lark",
        LarkTentacle(
            "lark",
            octomate,
            config=channel_config,
        ),
    )

if (channel_config := config.channels.napcat) is not None and channel_config.enabled:
    octomate.connect_channel(
        "napcat",
        NapcatTentacle(
            "napcat",
            octomate,
            config=channel_config,
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
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        log_level=config.logging.level.lower(),
    )
