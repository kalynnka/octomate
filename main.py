from __future__ import annotations

import logging

import logfire
import uvicorn

from octomate.config import OctomateConfig

config = OctomateConfig()
logfire.configure(
    service_name=config.logfire.service_name,
    environment=config.logfire.environment,
    send_to_logfire="if-token-present" if config.logfire.send_to_logfire else False,
    console=logfire.ConsoleOptions() if config.logfire.console else False,
    scrubbing=logfire.ScrubbingOptions(
        extra_patterns=[
            "conversation_key",
            "source_key",
            "target_key",
            "user_id",
            "chat_id",
            "responder_id",
            "message_id",
        ]
    )
    if config.logfire.scrub
    else False,
)
logfire.instrument_pydantic_ai()
logfire.instrument_httpx()
logfire.instrument_sqlalchemy()

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(config.logging.format))
logging.basicConfig(
    level=config.logging.level,
    handlers=[console_handler, logfire.LogfireLoggingHandler()],
    force=True,
)

from octomate import Octomate
from octomate.tentacles.agent.inkling import InklingTentacle, build_inkling_agent
from octomate.tentacles.channel.lark import LarkTentacle
from octomate.tentacles.channel.napcat import NapcatTentacle
from octomate.tentacles.channel.slack import SlackTentacle
from octomate.web.dev_ui import build_dev_ui_router

octomate = Octomate()
octomate.register_agent(
    "inkling",
    InklingTentacle(
        "inkling",
        octomate,
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
