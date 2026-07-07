from __future__ import annotations

import logging

import logfire
import uvicorn
from fastapi import FastAPI

from octomate import Octomate
from octomate.capabilities.history import HistoryCapability
from octomate.capabilities.send import SendCapability
from octomate.capabilities.todos import TodoCapability
from octomate.config import OctomateConfig
from octomate.providers import ProviderHttpLogFilter, ProviderRegistry
from octomate.tentacles.agent.claude import ClaudeCodeTentacle
from octomate.tentacles.agent.inkling import (
    InklingTentacle,
    build_mcp_toolsets,
    inkling_toolset,
)
from octomate.tentacles.agent.inkling.prompts import SYSTEM_PROMPT
from octomate.tentacles.base import TentacleLogFormatter
from octomate.tentacles.channel.lark import LarkTentacle
from octomate.tentacles.channel.napcat import NapcatTentacle
from octomate.tentacles.channel.slack import SlackTentacle
from octomate.tentacles.channel.web.vercel import VercelTentacle, build_vercel_router

config = OctomateConfig()


def create_app() -> FastAPI:
    """Build the Octomate FastAPI app.

    All setup lives here, not at import time, so that uvicorn's reload supervisor
    and worker processes can import this module without re-running logfire
    instrumentation, channel auth, and the rest. Only the process that actually
    serves calls the factory (`uvicorn main:create_app --factory`).
    """
    logfire.configure(
        service_name=config.logfire.service_name,
        environment=config.logfire.environment,
        send_to_logfire="if-token-present" if config.logfire.send_to_logfire else False,
        console=logfire.ConsoleOptions() if config.logfire.console else False,
        scrubbing=logfire.ScrubbingOptions(
            extra_patterns=[
                "conversation_address",
                "source_address",
                "target_address",
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

    octomate = Octomate()

    console_handler = logging.StreamHandler()
    # Tint the level + each tentacle's header, but only on a real terminal so the
    # ANSI codes don't leak into piped/redirected logs.
    console_handler.setFormatter(
        TentacleLogFormatter(octomate, colorize=console_handler.stream.isatty())
    )
    logging.basicConfig(
        level=config.logging.level,
        handlers=[console_handler, logfire.LogfireLoggingHandler()],
        force=True,
    )
    registry = ProviderRegistry(config.providers)
    # httpx logs every request at INFO; keep the LLM-provider round-trips and drop
    # the rest (Lark cardkit streaming PUTs especially). Logfire still traces every
    # request regardless. Per-logger overrides from config win.
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.setLevel(logging.INFO)
    httpx_logger.addFilter(ProviderHttpLogFilter(registry))
    for name, level in config.logging.loggers.items():
        logging.getLogger(name).setLevel(level)
    logger = logging.getLogger("octomate.main")

    octomate.connect(
        InklingTentacle(
            "inkling",
            octomate,
            models={
                model.name: registry.build_model(model)
                for model in config.agents.inkling.models
            },
            toolsets=[
                inkling_toolset,
                *build_mcp_toolsets(config.mcp),
            ],
            capabilities=[
                TodoCapability(),
                SendCapability(),
                HistoryCapability(octomate.conversations, octomate.thread_manager),
            ],
            system_prompt=SYSTEM_PROMPT,
        ),
    )

    if (claude_config := config.agents.claude) is not None:
        octomate.connect(ClaudeCodeTentacle("claude", octomate, config=claude_config))

    if (channel_config := config.channels.slack) is not None and channel_config.enabled:
        octomate.connect(
            SlackTentacle(
                "slack",
                octomate,
                config=channel_config,
            )
        )

    if (channel_config := config.channels.lark) is not None and channel_config.enabled:
        octomate.connect(
            LarkTentacle(
                "lark",
                octomate,
                config=channel_config,
            )
        )

    if (
        channel_config := config.channels.napcat
    ) is not None and channel_config.enabled:
        octomate.connect(
            NapcatTentacle(
                "napcat",
                octomate,
                config=channel_config,
            )
        )

    if (
        channel_config := config.channels.dev_ui
    ) is not None and channel_config.enabled:
        octomate.connect(
            VercelTentacle(
                "dev_ui",
                octomate,
                config=channel_config,
            )
        )
        octomate.include_router(build_vercel_router(octomate, channel_id="dev_ui"))
        logger.info(
            "Dev UI (vercel) enabled — open http://%s:%d/",
            config.host,
            config.port,
        )

    return octomate.app(title="Octomate")


if __name__ == "__main__":
    uvicorn.run(
        "main:create_app",
        factory=True,
        host=str(config.host),
        port=config.port,
        reload=True,
        log_level=config.logging.level.lower(),
    )
