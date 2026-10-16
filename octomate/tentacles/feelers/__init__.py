"""Feelers: the per-channel renderers a tentacle presents markdown, segments,
timelines, artifacts, deferred actions and authorizations through."""

from octomate.tentacles.feelers.artifacts import ArtifactFeeler, PlainTextArtifactFeeler
from octomate.tentacles.feelers.base import Feelers
from octomate.tentacles.feelers.deferred import (
    ApprovalFeeler,
    PlainTextApprovalFeeler,
    PlainTextAskQuestionFeeler,
    QuestionFeeler,
)
from octomate.tentacles.feelers.oauth import (
    OAuthFeeler,
    PlainTextOAuthFeeler,
)
from octomate.tentacles.feelers.output import (
    DefaultMarkdownFeeler,
    IMMessageID,
    MarkdownFeeler,
)

__all__ = [
    "ApprovalFeeler",
    "ArtifactFeeler",
    "DefaultMarkdownFeeler",
    "Feelers",
    "IMMessageID",
    "MarkdownFeeler",
    "OAuthFeeler",
    "PlainTextApprovalFeeler",
    "PlainTextArtifactFeeler",
    "PlainTextAskQuestionFeeler",
    "PlainTextOAuthFeeler",
    "QuestionFeeler",
]
