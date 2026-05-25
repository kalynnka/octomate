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
            app_id=channel_config.app_id,
            bot_token=channel_config.bot_token,
            app_token=channel_config.app_token,
            agent_id=channel_config.agent_id,
            mention_only=channel_config.mention_only,
        ),
    )

if (channel_config := config.channels.lark) is not None and channel_config.enabled:
    octomate.connect_channel(
        "lark",
        LarkTentacle(
            "lark",
            octomate,
            app_id=channel_config.app_id,
            app_secret=channel_config.app_secret,
            agent_id=channel_config.agent_id,
            mention_only=channel_config.mention_only,
        ),
    )

if (channel_config := config.channels.napcat) is not None and channel_config.enabled:
    octomate.connect_channel(
        "napcat",
        NapcatTentacle(
            "napcat",
            octomate,
            ws_url=channel_config.ws_url,
            http_url=channel_config.http_url,
            access_token=channel_config.access_token,
            backoff_base=channel_config.backoff_base,
            backoff_max=channel_config.backoff_max,
            backoff_factor=channel_config.backoff_factor,
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
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        log_level=config.logging.level.lower(),
    )
