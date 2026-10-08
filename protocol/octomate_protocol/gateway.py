"""Gateway tool names shared by the server and native-client installers."""

from enum import StrEnum

GATEWAY_NAMESPACE = "gateway"
GATEWAY_TOOLSET_ID = GATEWAY_NAMESPACE


class GatewayTool(StrEnum):
    """The tool names used by the gateway capability."""

    INSPECT = "inspect"
    SUMMON = "summon"
    TELEPORT = "teleport"
    SCHEME = "scheme"
    DISMISS = "dismiss"
    COMMISSION = "commission"
    WHISPER = "whisper"
    SEND = "send"


def gateway_tool(name: GatewayTool) -> str:
    """The MCP name of a gateway tool, including its namespace."""
    return f"{GATEWAY_NAMESPACE}_{name}"
