"""Shared artifact viewer paths and the browser sandbox origin."""

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)


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
    viewer_origin: AnyHttpUrl | None = Field(
        default=None,
        description=(
            "Browser-reachable HTTP(S) origin hosting the artifact viewer, without "
            "credentials, path, query, or fragment. Combined with path and the "
            "artifact UUID for links in channel overview cards. Required to build "
            "absolute viewer links; must differ from sandbox_origin."
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

    @field_validator("viewer_origin", "sandbox_origin")
    @classmethod
    def validate_origin(
        cls, value: AnyHttpUrl | None, info: ValidationInfo
    ) -> AnyHttpUrl | None:
        if value is not None and (
            value.username is not None
            or value.password is not None
            or value.path not in {None, "/"}
            or value.query is not None
            or value.fragment is not None
        ):
            raise ValueError(
                f"{info.field_name} must be an origin without credentials, path, query, or fragment"
            )
        return value

    @model_validator(mode="after")
    def separate_viewer_and_sandbox(self) -> "ArtifactConfig":
        if self.viewer_origin is not None and self.viewer_origin == self.sandbox_origin:
            raise ValueError("viewer_origin must differ from sandbox_origin")
        return self
