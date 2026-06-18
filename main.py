from __future__ import annotations

import logging

import logfire
import uvicorn
from fastapi import FastAPI
from pydantic_ai.tools import DeferredToolRequests

from octomate import Octomate
from octomate.capabilities.agent import Agent
from octomate.capabilities.history import HistoryCapability
from octomate.capabilities.send import SendCapability
from octomate.capabilities.todos import TodoCapability
from octomate.config import OctomateConfig
from octomate.providers import ProviderRegistry
from octomate.schemas.segments import MessageSegment
from octomate.tentacles.agent.claude import ClaudeCodeTentacle
from octomate.tentacles.agent.inkling import (
    InklingTentacle,
    build_mcp_toolsets,
    inkling_toolset,
)
from octomate.tentacles.agent.inkling.prompts import SYSTEM_PROMPT
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
    logger = logging.getLogger("octomate.main")

    octomate = Octomate()
    registry = ProviderRegistry(config.providers)

    inkling_agent = Agent(
        registry.build_model(config.agents.inkling.model),
        deps_type=type(None),
        name="octomate-inkling",
        output_type=[str, list[MessageSegment], DeferredToolRequests],
        toolsets=[
            inkling_toolset,
            *build_mcp_toolsets(config.mcp),
        ],
        capabilities=[
            TodoCapability(),
            SendCapability(),
            HistoryCapability(octomate.conversations),
        ],
        system_prompt=SYSTEM_PROMPT,
    )
    octomate.register_agent(
        "inkling",
        InklingTentacle(
            "inkling",
            octomate,
            agent=inkling_agent,
            conversation_manager=octomate.conversations,
        ),
    )

    if (claude_config := config.agents.claude) is not None:
        octomate.register_agent(
            "claude",
            ClaudeCodeTentacle("claude", octomate, config=claude_config),
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

    if (
        channel_config := config.channels.napcat
    ) is not None and channel_config.enabled:
        octomate.connect_channel(
            "napcat",
            NapcatTentacle(
                "napcat",
                octomate,
                config=channel_config,
            ),
        )

    if (
        channel_config := config.channels.dev_ui
    ) is not None and channel_config.enabled:
        octomate.connect_channel(
            "dev_ui",
            VercelTentacle(
                "dev_ui",
                octomate,
                config=channel_config,
            ),
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
