from pydantic import BaseModel, Field


class McpPoolConfig(BaseModel):
    idle_timeout: float = Field(
        default=60 * 60.0,
        gt=0,
        allow_inf_nan=False,
        description=(
            "Seconds since the latest acquisition before a cached MCP client is "
            "closed. Each acquisition renews the cleanup timer."
        ),
    )
