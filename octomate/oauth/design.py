"""Shared Trunkline design assets for OAuth browser pages."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse

# Wheels carry the shared design assets; editable checkouts use their source.
DESIGN_ROOT = Path(__file__).parent.parent / "static/authorization"
if not DESIGN_ROOT.is_dir():
    DESIGN_ROOT = Path(__file__).parent.parent.parent / "trunkline"
DESIGN_STYLE = "\n".join(
    line
    for line in (DESIGN_ROOT / "src/styles/lonetrail.css").read_text().splitlines()
    if not line.startswith("@import")
).replace('url("/fonts/', 'url("/oauth/design/fonts/')


design_router = APIRouter(prefix="/oauth/design", tags=["oauth"])


@design_router.get("/lonetrail.css", include_in_schema=False)
async def design_style() -> Response:
    """Use Trunkline's tokens and local fonts without third-party font requests."""
    return Response(DESIGN_STYLE, media_type="text/css")


@design_router.get("/fonts/{name}", include_in_schema=False)
async def design_font(name: str) -> FileResponse:
    path = DESIGN_ROOT / "public/fonts" / name
    if (
        Path(name).name != name
        or path.suffix not in {".otf", ".woff2"}
        or not path.is_file()
    ):
        raise HTTPException(404, "Font not found")
    return FileResponse(path)
