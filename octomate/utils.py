from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from contextvars import copy_context
from mimetypes import guess_extension, guess_type

import anyio

logger = logging.getLogger(__name__)


async def drain_task[ResultT](task: asyncio.Future[ResultT]) -> bool:
    """Finish a task despite caller cancellation; report whether it was deferred.

    The caller must re-raise CancelledError after its remaining cleanup. Task
    failures, including cancellation of the task itself, propagate normally.
    AnyIO level cancellation is shielded; repeated asyncio cancellation is caught.
    """
    cancelled = False
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
    task.result()
    return cancelled


async def acquire_in_thread[ResultT](
    call: Callable[[], ResultT], *, discard: Callable[[ResultT], None]
) -> ResultT:
    """Run a blocking call with context propagation and dispose of abandoned results.

    Caller cancellation returns immediately without cancelling the worker.
    `discard` runs on the event loop if the result can no longer be returned.
    Late failures are retrieved and logged; successful callers own their result.
    """
    operation = asyncio.get_running_loop().run_in_executor(
        None, copy_context().run, call
    )
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:

        def complete(completed: asyncio.Future[ResultT]) -> None:
            try:
                discard(completed.result())
            except Exception:
                logger.debug("Cancelled blocking call cleanup failed", exc_info=True)

        operation.add_done_callback(complete)
        raise


def guess_image_ext(content_type: str, url: str) -> str:
    ct = content_type.split(";")[0].strip()
    ext = guess_extension(ct) if ct.startswith("image/") else None
    if not ext:
        ext = guess_extension(guess_type(url)[0] or "") or ".png"
    return ext


def strip_markdown(text: str) -> str:
    text = re.sub(r"^```\w*\n?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^```$", "", text, flags=re.MULTILINE)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"\*{3}(.+?)\*{3}", r"\1", text)
    text = re.sub(r"_{3}(.+?)_{3}", r"\1", text)
    text = re.sub(r"\*{2}(.+?)\*{2}", r"\1", text)
    text = re.sub(r"_{2}(.+?)_{2}", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1 (\2)", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    return text
