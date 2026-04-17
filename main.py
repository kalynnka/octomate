from __future__ import annotations

import asyncio
import logging
import warnings
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import octotools

warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"websockets")
warnings.filterwarnings("ignore", category=SyntaxWarning, module=r"zep_cloud")

from octomate.config import (
    ClaudeCodeConfig,
    CopilotConfig,
    DeepResearchConfig,
    FastResearchConfig,
    LarkTentacleConfig,
    NapcatTentacleConfig,
    OctomateConfig,
    PulseAgentConfig,
    SlackTentacleConfig,
)
from octomate.memory import Mem0Memory, OctopusMemory, ZepMemory
from octomate.octopus import Octopus
from octomate.tentacles.agent.pulse import PulseTentacle
from octomate.tentacles.agent.skills import SkillManager, build_skill_manager

logging.basicConfig(level=logging.INFO)
logging.getLogger("watchfiles").setLevel(logging.WARNING)


def _build_octopus() -> Octopus:
    config = OctomateConfig()

    skill_manager = SkillManager()
    octotools.streamify.register(skill_manager)
    octotools.github.register(skill_manager)
    octotools.linear.register(skill_manager)
    octotools.tarot.register(skill_manager)

    octopus = Octopus(skill_manager=skill_manager)

    for tag, ac in config.agents.items():
        if isinstance(ac, PulseAgentConfig):
            mgr, distiller = build_skill_manager(ac, config.workdir)
            octotools.streamify.register(mgr)
            octotools.github.register(mgr)
            octotools.linear.register(mgr)
            octotools.tarot.register(mgr)
            tentacle = PulseTentacle(tag, octopus, ac, mgr)
            tentacle.distiller = distiller
            octopus.graft(tentacle)
        elif isinstance(ac, ClaudeCodeConfig):
            from octomate.tentacles.agent.claude import ClaudeCodeTentacle

            octopus.graft(ClaudeCodeTentacle(tag, octopus, ac))
        elif isinstance(ac, CopilotConfig):
            from octomate.tentacles.agent.copilot import CopilotTentacle

            octopus.graft(CopilotTentacle(tag, octopus, ac))
        elif isinstance(ac, FastResearchConfig):
            from octomate.tentacles.agent.research import FastResearchTentacle

            octopus.graft(FastResearchTentacle(tag, octopus, ac))
        elif isinstance(ac, DeepResearchConfig):
            from octomate.tentacles.agent.research import DeepResearchTentacle

            octopus.graft(DeepResearchTentacle(tag, octopus, ac))

    pulse = next(
        (t for t in octopus.agent_tentacles.values() if isinstance(t, PulseTentacle)),
        None,
    )

    if not config.tentacles:
        return octopus
    if pulse is None:
        raise RuntimeError(
            "Pulse agent is required when tentacles are configured. "
            "Add a pulse entry under agents: in your octomate.yaml."
        )

    for name, tc in config.tentacles.items():
        mem = tc.memory
        if mem.mem0.enabled:
            memory = Mem0Memory(config=mem.mem0)
        elif mem.zep.enabled:
            memory = ZepMemory(api_key=mem.zep.api_key)
        else:
            memory = OctopusMemory()

        if isinstance(tc, NapcatTentacleConfig):
            from octomate.tentacles.channel.napcat import NapcatTentacle

            octopus.connect(
                NapcatTentacle(
                    name,
                    octopus,
                    ws_url=tc.ws_url,
                    http_url=str(tc.http_url),
                    access_token=tc.access_token,
                    backoff_base=tc.backoff_base,
                    backoff_max=tc.backoff_max,
                    backoff_factor=tc.backoff_factor,
                    pulse=pulse,
                    memory=memory,
                    flush_delay=tc.flush_delay,
                )
            )
        elif isinstance(tc, LarkTentacleConfig):
            from octomate.tentacles.channel.lark import LarkTentacle

            warnings.filterwarnings(
                "ignore", category=DeprecationWarning, module=r"lark_oapi"
            )
            octopus.connect(
                LarkTentacle(
                    name,
                    octopus,
                    app_id=tc.app_id,
                    app_secret=tc.app_secret,
                    pulse=pulse,
                    memory=memory,
                    flush_delay=tc.flush_delay,
                )
            )
        elif isinstance(tc, SlackTentacleConfig):
            from octomate.tentacles.channel.slack import SlackTentacle

            octopus.connect(
                SlackTentacle(
                    name,
                    octopus,
                    bot_token=tc.bot_token,
                    app_token=tc.app_token,
                    pulse=pulse,
                    memory=memory,
                    flush_delay=tc.flush_delay,
                )
            )

    return octopus


async def app(
    scope: MutableMapping[str, Any],
    receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
    send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
) -> None:
    if scope["type"] != "lifespan":
        return
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            try:
                octopus = _build_octopus()
                asyncio.create_task(octopus.activate())
                await send({"type": "lifespan.startup.complete"})
            except Exception as e:
                logging.error(f"Error during startup: {e}")
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return
