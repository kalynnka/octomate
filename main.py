from __future__ import annotations

import asyncio
import logging
import warnings

import octotools

warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"websockets")
warnings.filterwarnings("ignore", category=SyntaxWarning, module=r"zep_cloud")

from octomate.agents.manager import SkillManager
from octomate.agents.pulse import build_summon_toolset, create_pulse_agents
from octomate.config import (
    ClaudeCodeConfig,
    CopilotConfig,
    LarkTentacleConfig,
    NapcatTentacleConfig,
    OctomateConfig,
    SlackTentacleConfig,
)
from octomate.memory import Mem0Memory, OctopusMemory, ZepMemory
from octomate.octopus import Octopus

logging.basicConfig(level=logging.INFO)
logging.getLogger("watchfiles").setLevel(logging.WARNING)


def _build_octopus() -> Octopus:
    config = OctomateConfig()

    skill_manager = SkillManager()
    octotools.streamify.register(skill_manager)
    octotools.github.register(skill_manager)
    octotools.linear.register(skill_manager)
    octotools.tarot.register(skill_manager)

    napcat_skill_manager = SkillManager()
    octotools.streamify.register(napcat_skill_manager)
    octotools.tarot.register(napcat_skill_manager)

    octopus = Octopus(skill_manager=skill_manager)

    for ac in config.agents:
        if isinstance(ac, ClaudeCodeConfig):
            from octomate.tentacles.claude import ClaudeCodeTentacle

            octopus.graft(ClaudeCodeTentacle(ac.tag, octopus, ac))
        elif isinstance(ac, CopilotConfig):
            from octomate.tentacles.copilot import CopilotTentacle

            octopus.graft(CopilotTentacle(ac.tag, octopus, ac))

    summon_toolset = build_summon_toolset(octopus.agent_tentacles)

    for tc in config.tentacles:
        mem = tc.memory
        if mem.mem0.enabled:
            memory = Mem0Memory(config=mem.mem0)
        elif mem.zep.enabled:
            memory = ZepMemory(api_key=mem.zep.api_key)
        else:
            memory = OctopusMemory()

        if isinstance(tc, NapcatTentacleConfig):
            from octomate.tentacles.napcat import NapcatTentacle

            octopus.connect(
                NapcatTentacle(
                    tc.name,
                    octopus,
                    ws_url=tc.ws_url,
                    http_url=str(tc.http_url),
                    access_token=tc.access_token,
                    backoff_base=tc.backoff_base,
                    backoff_max=tc.backoff_max,
                    backoff_factor=tc.backoff_factor,
                    agents=create_pulse_agents(
                        tc.pulse,
                        napcat_skill_manager,
                    ),
                    memory=memory,
                    flush_delay=tc.flush_delay,
                )
            )
        elif isinstance(tc, LarkTentacleConfig):
            from octomate.tentacles.lark import LarkTentacle

            warnings.filterwarnings(
                "ignore", category=DeprecationWarning, module=r"lark_oapi"
            )
            octopus.connect(
                LarkTentacle(
                    tc.name,
                    octopus,
                    app_id=tc.app_id,
                    app_secret=tc.app_secret,
                    agents=create_pulse_agents(
                        tc.pulse,
                        skill_manager,
                        summon_toolset=summon_toolset,
                    ),
                    memory=memory,
                    flush_delay=tc.flush_delay,
                )
            )
        elif isinstance(tc, SlackTentacleConfig):
            from octomate.tentacles.slack import SlackTentacle

            octopus.connect(
                SlackTentacle(
                    tc.name,
                    octopus,
                    bot_token=tc.bot_token,
                    app_token=tc.app_token,
                    agents=create_pulse_agents(
                        tc.pulse,
                        skill_manager,
                        summon_toolset=summon_toolset,
                    ),
                    memory=memory,
                    flush_delay=tc.flush_delay,
                )
            )

    return octopus


async def app(scope, receive, send):
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
