import pytest
from pydantic_ai import models as pai_models

from octomate.database import init_db

pai_models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture(autouse=True)
async def _setup_db(tmp_path):
    """Initialize a fresh in-memory test database for each test."""
    import octomate.database as db_mod

    db_mod._engine = None
    await init_db(tmp_path / "test.db")
