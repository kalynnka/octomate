from __future__ import annotations

import logging
import sys

import anyio
from dotenv import load_dotenv

from octomate.base import Octopus
from octomate.config import OctomateConfig
from octomate.nerve import OctopusNerve
from octomate.tentacles.napcat import NapcatTentacle

load_dotenv()
logging.basicConfig(level=logging.INFO)
logging.getLogger("watchfiles").setLevel(logging.WARNING)


def run() -> None:
    if "--reload" in sys.argv:
        from watchfiles import run_process

        run_process(
            "octomate",
            target="python -m octomate.main",
            target_type="command",
        )
    else:
        config = OctomateConfig()
        octopus = Octopus(
            OctopusNerve(flush_delay=config.brain.flush_delay),
            config.brain,
        )
        for tc in config.tentacles:
            octopus.connect(NapcatTentacle(tc))
        try:
            anyio.run(octopus.activate)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    run()
