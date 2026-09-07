from itertools import cycle
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai_codex.generated.v2_all import ConfigReadResponse, ModelListResponse

from octomate.tentacles.codex import base as codex_base


@pytest.fixture(autouse=True)
def codex_catalog(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Catalog reads never launch a real app-server in agent tests."""
    client = AsyncMock()
    client.request.side_effect = cycle(
        [
            ConfigReadResponse.model_validate({"config": {}, "origins": {}}),
            ModelListResponse.model_validate(
                {
                    "data": [
                        {
                            "id": "test-model",
                            "model": "test-model",
                            "displayName": "Test model",
                            "description": "Test catalog",
                            "isDefault": True,
                            "hidden": False,
                            "defaultReasoningEffort": "medium",
                            "supportedReasoningEfforts": [
                                {"reasoningEffort": "medium", "description": "Balanced"}
                            ],
                        }
                    ],
                }
            ),
        ]
    )
    transport = MagicMock()
    transport.__aenter__.return_value = client
    monkeypatch.setattr(
        codex_base, "AsyncCodexClient", MagicMock(return_value=transport)
    )
    return client
