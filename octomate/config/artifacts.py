"""Shared artifact viewer paths and the browser sandbox origin."""

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator


class ArtifactConfig(BaseModel):
    """Deployment-wide locations for artifact viewers and their isolated runtime."""

    path: str = Field(
        default="/artifacts",
        pattern=r"^/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$",
        description=(
            "Root-relative URL prefix for artifact viewers on the console host. "
            "Use slash-separated segments of letters, digits, hyphens, or underscores, "
            "without a trailing slash. Each viewer appends the artifact UUID."
        ),
    )
    sandbox_origin: AnyHttpUrl | None = Field(
        default=None,
        description=(
            "Shared HTTP(S) origin for running artifact HTML, without credentials, "
            "path, query, or fragment. Must differ from the console origin; a separate "
            "hostname or port can provide this. Expose only sandbox routes and assets "
            "on this origin. This setting controls browser hosting only."
        ),
    )

    @field_validator("sandbox_origin")
    @classmethod
    def validate_sandbox_origin(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is not None and (
            value.username is not None
            or value.password is not None
            or value.path not in {None, "/"}
            or value.query is not None
            or value.fragment is not None
        ):
            raise ValueError(
                "sandbox_origin must be an origin without credentials, path, query, or fragment"
            )
        return value
