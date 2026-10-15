"""Database-free sandbox server for the isolated Firefox suite."""

from pydantic import AnyHttpUrl
from starlette.applications import Starlette

from octomate.artifacts import ArtifactSandbox

app = ArtifactSandbox(Starlette(), origin=AnyHttpUrl("http://127.0.0.1:8765"))
