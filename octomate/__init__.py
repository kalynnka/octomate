import warnings

# `ExternalAgentRun.kind` narrows the polymorphic discriminator on purpose, which pydantic
# flags with a UserWarning as the run schema imports (see octomate/schemas/runs.py). The
# override is intentional, so silence it here at the package boundary — before the schemas
# import below — so it does not fire on every entrypoint (the CLI especially).
warnings.filterwarnings(
    "ignore",
    message='Field name "kind" in "ExternalAgentRun" shadows',
    category=UserWarning,
)

# The channel profile schemas intentionally redeclare UserProfile fields to attach
# platform aliases and defaults. Since UserProfile became a transmuter, its metaclass
# serves same-named class attributes (column accessors), which pydantic reports as
# shadowing on every redeclaration.
warnings.filterwarnings(
    "ignore",
    message=r'Field name "\w+" in "\w*(UserProfile|Sender)" shadows',
    category=UserWarning,
)

from octomate.base import Octomate  # noqa: E402  (filter must precede this import)

__all__ = ["Octomate"]
