import warnings

from pydantic_graph import PydanticGraphDeprecationWarning

# The react/triage graphs intentionally use pydantic-graph's deprecated v1 BaseNode
# `Graph` API (pinned <2 in pyproject) until the v2 GraphBuilder API is stable;
# silence its import-time deprecation warning. Set here, before any octomate
# submodule imports `Graph`, so it covers every entrypoint (app and tests).
warnings.filterwarnings("ignore", category=PydanticGraphDeprecationWarning)

from octomate.base import Octomate  # noqa: E402  (filter must precede this import)

__all__ = ["Octomate"]
