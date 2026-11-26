"""ACP runtime session capabilities."""

from typing import ClassVar

from octomate.tentacles.agent import AgentInk


class AcpInk(AgentInk):
    """ACP session forking is not implemented."""

    supports_session_fork: ClassVar[bool] = False
