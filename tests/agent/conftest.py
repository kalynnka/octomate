import asyncio
from itertools import cycle
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai_codex.errors import TransportClosedError
from openai_codex.generated.v2_all import ConfigReadResponse, ModelListResponse
from openai_codex.models import Notification

from octomate.tentacles.codex import ink as codex_ink


@pytest.fixture(autouse=True)
def codex_catalog(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Catalog reads never launch a real app-server in agent tests."""
    client = AsyncMock()
    closed = asyncio.Event()

    async def receive() -> Notification:
        await closed.wait()
        raise TransportClosedError("closed")

    client.start.side_effect = closed.clear
    client.close.side_effect = closed.set
    client.next_notification.side_effect = receive
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
    runtime = AsyncMock()
    runtime._client = client

    async def enter() -> AsyncMock:
        await client.start()
        await client.initialize()
        return runtime

    runtime.__aenter__.side_effect = enter
    runtime.close = client.close
    monkeypatch.setattr(codex_ink, "SharedCodex", MagicMock(return_value=runtime))
    return client
