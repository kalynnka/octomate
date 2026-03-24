from __future__ import annotations

import asyncio
import logging
import sys
import warnings

import octotools

warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"websockets")
warnings.filterwarnings("ignore", category=SyntaxWarning, module=r"zep_cloud")

from octomate.agents.flick import create_flick_agent
from octomate.agents.manager import SkillManager
from octomate.agents.surge import create_surge_agent
from octomate.config import (
    LarkTentacleConfig,
    NapcatTentacleConfig,
    OctomateConfig,
    SlackTentacleConfig,
)
from octomate.memory import Mem0Memory, OctopusMemory, ZepMemory
from octomate.octopus import Octopus

logging.basicConfig(level=logging.INFO)
logging.getLogger("watchfiles").setLevel(logging.WARNING)


def _start() -> None:
    config = OctomateConfig()

    skill_manager = SkillManager()
    octotools.streamify.register(skill_manager)
    octotools.github.register(skill_manager)
    octotools.linear.register(skill_manager)

    if config.surge.base_url:
        import os

        os.environ.setdefault("GOOGLE_GEMINI_BASE_URL", config.surge.base_url)

    agent = create_surge_agent(config.surge, skill_manager)
    octopus = Octopus(agent, skill_manager=skill_manager)

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
                    flick=create_flick_agent(tc.flick, skill_manager),
                    memory=memory,
                    flush_delay=config.surge.flush_delay,
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
                    store=octopus.store,
                    flick=create_flick_agent(tc.flick, skill_manager),
                    memory=memory,
                    flush_delay=config.surge.flush_delay,
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
                    store=octopus.store,
                    flick=create_flick_agent(tc.flick, skill_manager),
                    memory=memory,
                    flush_delay=config.surge.flush_delay,
                )
            )

    try:
        asyncio.run(octopus.activate())
    except KeyboardInterrupt:
        pass


def run() -> None:
    if "--reload" in sys.argv:
        from watchfiles import run_process

        run_process(
            "octomate",
            target="main._start",
            target_type="function",
        )
    else:
        _start()


if __name__ == "__main__":
    run()
