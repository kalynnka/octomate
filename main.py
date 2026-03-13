from __future__ import annotations

import asyncio
import logging
import sys

import octotools
from octomate.agents.manager import SkillManager
from octomate.config import OctomateConfig
from octomate.mind import Octopus
from octomate.nerve import OctopusNerve
from octomate.tentacles.napcat import NapcatTentacle

logging.basicConfig(level=logging.INFO)
logging.getLogger("watchfiles").setLevel(logging.WARNING)


def _start() -> None:
    config = OctomateConfig()

    skill_manager = SkillManager()

    octotools.qweather.register(skill_manager)
    octotools.pixiv.register(skill_manager)

    octopus = Octopus(
        OctopusNerve(flush_delay=config.brain.flush_delay),
        config.brain,
        skill_manager=skill_manager,
    )
    for tc in config.tentacles:
        octopus.connect(NapcatTentacle(tc))
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
