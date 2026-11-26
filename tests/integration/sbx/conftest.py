"""Run disposable VM cleanup in pytest's separate teardown phase."""

from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack

import pytest


@pytest.fixture
async def sbx_cleanup() -> AsyncGenerator[AsyncExitStack]:
    """Attempt every callback, reporting cleanup errors apart from test failures."""
    async with AsyncExitStack() as cleanup:
        yield cleanup
