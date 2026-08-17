from __future__ import annotations

from typing import TYPE_CHECKING

from octomate.config.channels import (
    ChannelConfigVariant,
    LarkChannelConfig,
    NapcatChannelConfig,
    SlackChannelConfig,
    TrunklineChannelConfig,
)
from octomate.tentacles.channels.base import (
    ChannelTentacle,
    Chromo,
    DownloadedImage,
    Ink,
)

if TYPE_CHECKING:
    from octomate import Octomate


def build_channel(
    id: str,
    config: ChannelConfigVariant,
    octomate: Octomate,
) -> ChannelTentacle:
    """Compose one configured channel into its tentacle.

    The one place a `type:` becomes a platform, mirroring `build_integration`. The
    configured key is the tentacle id throughout, which is what lets one platform be
    mounted more than once — two Lark apps are two keys, and nothing below here
    learns that they share a class.

    Imported inside the function because each platform module pulls in its vendor
    SDK, and importing this package must not cost all four.
    """
    match config:
        case SlackChannelConfig():
            from octomate.tentacles.channels.slack import SlackTentacle

            return SlackTentacle(id, octomate, config=config)
        case LarkChannelConfig():
            from octomate.tentacles.channels.lark import LarkTentacle

            return LarkTentacle(id, octomate, config=config)
        case NapcatChannelConfig():
            from octomate.tentacles.channels.napcat import NapcatTentacle

            return NapcatTentacle(id, octomate, config=config)
        case TrunklineChannelConfig():
            from octomate.tentacles.channels.web.trunkline import TrunklineTentacle

            return TrunklineTentacle(id, octomate, config=config)


__all__ = [
    "ChannelTentacle",
    "Chromo",
    "DownloadedImage",
    "Ink",
    "build_channel",
]
