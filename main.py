from __future__ import annotations

import logging

import logfire
import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr
from pydantic_ai import AgentCapability

from octomate import Octomate
from octomate.capabilities.ask import AskCapability
from octomate.capabilities.github import GitHubCapability
from octomate.capabilities.history import HistoryCapability
from octomate.capabilities.todos import TodoCapability
from octomate.capabilities.tools import ToolFailureCapability
from octomate.config import OctomateConfig
from octomate.database import engine as db_engine
from octomate.managers.oauth import OAuthConnector
from octomate.managers.user import UserManager
from octomate.oauth.github import GitHubDeviceOAuthFlow
from octomate.providers import ProviderHttpLogFilter, ProviderRegistry
from octomate.tentacles.agent.claude import ClaudeCodeTentacle
from octomate.tentacles.agent.codex import CodexTentacle
from octomate.tentacles.agent.inkling import InklingTentacle, build_mcp_toolsets
from octomate.tentacles.agent.inkling.prompts import SYSTEM_PROMPT
from octomate.tentacles.base import TentacleLogFormatter
from octomate.tentacles.channel.lark import LarkTentacle
from octomate.tentacles.channel.napcat import NapcatTentacle
from octomate.tentacles.channel.slack import SlackTentacle
from octomate.tentacles.channel.web.vercel import VercelTentacle

config = OctomateConfig()


def hook_secret(agent: str) -> SecretStr:
    """The credential `agent`'s hook router authenticates against.

    Demanded rather than defaulted: these routers write a session's prompts and answers
    into thread history, which agents read back, so serving one without a credential
    would let anything that can reach the port speak as the human. Refusing to boot is
    the only honest answer — the alternative is an open router nobody notices.

    Asked for only where a hook-serving tentacle is actually being built, so a
    deployment that configures neither Claude nor Codex needs no secret at all.
    """
    if config.hook_secret is None:
        raise RuntimeError(
            f"octomate.hook_secret is unset, but agents.{agent} serves a hook router "
            "that authenticates against it. Run `octomate hooks secret` to generate one "
            f"and place it, then re-run `octomate {agent} hooks install`."
        )
    return config.hook_secret


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
    # Library auto-instrumentation is opt-in per library (diagnostic-session volume);
    # Octomate's own octomate.*-scoped spans are not behind these flags.
    if config.logfire.instrument.pydantic_ai:
        logfire.instrument_pydantic_ai()
    if config.logfire.instrument.httpx:
        logfire.instrument_httpx()
    if config.logfire.instrument.sqlalchemy:
        logfire.instrument_sqlalchemy(engine=db_engine())

    octomate = Octomate(
        users=UserManager(config.users),
        oauth_encryption_key=config.oauth.encryption_key,
    )

    inkling_capabilities: list[AgentCapability[None]] = [
        ToolFailureCapability(),
        AskCapability(),
        TodoCapability(
            id="todos",
            description="Persisted task list for planning and tracking "
            "multi-step work.",
            defer_loading=True,
        ),
        HistoryCapability(
            octomate.conversations,
            octomate.thread_manager,
            id="history",
            description="Search and page this thread's chat ledger and "
            "this conversation's model ledger.",
            defer_loading=True,
        ),
    ]
    # One capability for the whole deployment; each run mounts its own copy of it,
    # bound to the user that run is answering.
    if (
        github_config := config.integrations.github
    ) is not None and github_config.enabled:
        connector = OAuthConnector(
            id=github_config.id,
            flow=GitHubDeviceOAuthFlow(
                client_id=github_config.client_id,
                scopes=github_config.scopes,
            ),
        )
        octomate.oauth.register(connector)
        inkling_capabilities.append(
            GitHubCapability(
                manager=octomate.oauth,
                connector=connector,
                mcp_config=github_config.mcp,
                max_cached_users=github_config.max_cached_users,
                id=github_config.id,
                defer_loading=True,
            )
        )

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
    # watchfiles logs a line per change detected, and the Claude transcript tailer keeps
    # a watch on a directory its client rewrites through every turn — a stream of "1
    # change detected" saying only that a file moved. What came of the change is the
    # session's own story, which the hook and tailer lines tell.
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
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
            claims=config.agents.inkling.claims,
            toolsets=build_mcp_toolsets(config.mcp),
            capabilities=inkling_capabilities,
            system_prompt=SYSTEM_PROMPT,
        ),
    )

    if (claude_config := config.agents.claude) is not None:
        octomate.connect(
            ClaudeCodeTentacle(
                "claude",
                octomate,
                config=claude_config,
                hook_secret=hook_secret("claude"),
            )
        )

    if (codex_config := config.agents.codex) is not None and codex_config.enabled:
        octomate.connect(
            CodexTentacle(
                "codex",
                octomate,
                config=codex_config,
                hook_secret=hook_secret("codex"),
            )
        )

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
