from __future__ import annotations

from mimetypes import guess_extension, guess_type


def guess_image_ext(content_type: str, url: str) -> str:
    ct = content_type.split(";")[0].strip()
    ext = guess_extension(ct) if ct.startswith("image/") else None
    if not ext:
        ext = guess_extension(guess_type(url)[0] or "") or ".png"
    return ext
