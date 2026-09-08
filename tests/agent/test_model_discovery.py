from unittest.mock import AsyncMock, MagicMock

import pytest
from openai_codex.generated.v2_all import (
    ConfigReadResponse,
    ModelListResponse,
)
from openai_codex.generated.v2_all import (
    Model as CodexModel,
)
from pydantic_ai.models.test import TestModel
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.agents import Claim, ClaudeCodeConfig, CodexConfig, DeepseekConfig
from octomate.config.channels import TrunklineChannelConfig
from octomate.tentacles.agent import AgentTentacle
from octomate.tentacles.claude import ClaudeCodeTentacle
from octomate.tentacles.claude import base as claude_base
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.deepseek import DeepseekTentacle
from octomate.tentacles.deepseek.wire import OkResult
from octomate.tentacles.inkling import InklingTentacle
from octomate.tentacles.trunkline import TrunklineTentacle
from tests.agent.test_deepseek_tentacle import (
    KEY,
    FakeDeepseekApi,
    patch_gateway,
    turn_events,
)
from tests.support.managers import FakeConversationManager


@pytest.mark.parametrize("provider", [None, "firstParty", "bedrock"])
async def test_claude_uses_native_metadata_before_config(
    monkeypatch: pytest.MonkeyPatch, provider: str | None
) -> None:
    client = AsyncMock()
    client.get_server_info.return_value = {
        "account": {"apiProvider": provider},
        "models": [
            {
                "value": "future-model",
                "displayName": "Future",
                "description": "From the harness",
                "supportedEffortLevels": ["low", "high"],
            },
            {"value": "default", "displayName": "Account default"},
            {"value": "small", "displayName": "Small", "supportsEffort": False},
        ],
    }
    client.__aenter__.return_value = client
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", MagicMock(return_value=client))
    prefix = "bedrock" if provider == "bedrock" else "anthropic"
    configured = Claim("Configured fallback", efforts=("medium",))
    tentacle = ClaudeCodeTentacle(
        "claude",
        Octomate(),
        config=ClaudeCodeConfig(
            claims={
                f"{prefix}:future-model": configured,
                f"{prefix}:default": configured,
                f"{prefix}:small": configured,
            }
        ),
    )

    async with tentacle:
        assert tentacle.default_model is None
        assert tentacle.models[f"{prefix}:default"] == "default"
        assert tentacle.claims[f"{prefix}:future-model"] == Claim(
            "From the harness", efforts=("minimal", "low", "high")
        )
        assert tentacle.claims[f"{prefix}:default"] == configured
        assert tentacle.claims[f"{prefix}:small"].efforts == ()
        assert [route.claim.efforts for route in tentacle.routes] == [
            ("minimal", "low", "high"),
            ("medium",),
            (),
        ]
    client.query.assert_not_called()


def codex_model(
    name: str, *, default: bool = False, hidden: bool = False
) -> CodexModel:
    return CodexModel.model_validate(
        {
            "id": name,
            "model": name,
            "displayName": name,
            "description": "Native description",
            "isDefault": default,
            "hidden": hidden,
            "defaultReasoningEffort": "high",
            "supportedReasoningEfforts": [
                {"reasoningEffort": "high", "description": "Deep"}
            ],
        }
    )


@pytest.mark.parametrize(
    ("configured_model", "provider"),
    [
        ("future-model", "custom"),
        (None, None),
        ("unlisted-model", None),
    ],
)
async def test_codex_reads_provider_configured_default_and_every_catalog_page(
    codex_catalog: AsyncMock,
    configured_model: str | None,
    provider: str | None,
) -> None:
    codex_catalog.request.side_effect = [
        ConfigReadResponse.model_validate(
            {
                "config": {"model": configured_model, "model_provider": provider},
                "origins": {},
            }
        ),
        ModelListResponse(
            data=[codex_model("recommended", default=True)], next_cursor="page-2"
        ),
        ModelListResponse(
            data=[codex_model("future-model"), codex_model("hidden", hidden=True)]
        ),
    ]
    prefix = provider or "openai"
    tentacle = CodexTentacle(
        "codex",
        Octomate(),
        config=CodexConfig(
            claims={f"{prefix}:future-model": Claim("Old metadata", efforts=("low",))}
        ),
    )
    await tentacle.discover_models()

    assert tentacle.models == {
        f"{prefix}:recommended": "recommended",
        f"{prefix}:future-model": "future-model",
    }
    assert tentacle.default_model is None
    assert tentacle.provider == prefix
    assert tentacle.claims[f"{prefix}:future-model"] == Claim(
        "Native description", efforts=("high",)
    )
    assert [route.claim.efforts for route in tentacle.routes] == [("high",), ("high",)]
    assert codex_catalog.request.call_args_list[2].args == (
        "model/list",
        {"includeHidden": False, "cursor": "page-2"},
    )
    codex_catalog.initialize.assert_awaited_once()


async def test_codex_empty_catalog_fails_without_fabricating_models(
    codex_catalog: AsyncMock,
) -> None:
    codex_catalog.request.side_effect = [
        ConfigReadResponse.model_validate({"config": {}, "origins": {}}),
        ModelListResponse(data=[]),
    ]
    tentacle = CodexTentacle("codex", Octomate(), config=CodexConfig())
    with pytest.raises(ValueError, match="no available models"):
        await tentacle.discover_models()
    assert tentacle.models == {}


async def test_deepseek_preserves_provider_pairs_and_native_effort_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset(turn_events())
    FakeDeepseekApi.results["host.describe"] = OkResult(
        value={"provider": "second", "model": "future-model"}
    )
    FakeDeepseekApi.results["llm.models"] = OkResult(
        value={
            "groups": [
                {
                    "id": provider,
                    "name": provider,
                    "models": [
                        {
                            "id": "future-model",
                            "name": "Future",
                            "description": "Native ability",
                            "reasoning": {"efforts": [{"id": "low"}, {"id": "max"}]},
                        }
                    ],
                }
                for provider in ("first", "second")
            ],
            "failures": [],
        }
    )
    tentacle = DeepseekTentacle(
        "deepseek",
        Octomate(conversations=FakeConversationManager()),
        config=DeepseekConfig(),
    )
    async with tentacle:
        assert set(tentacle.models) == {"first:future-model", "second:future-model"}
        assert tentacle.default_model is None
        assert tentacle.claims["first:future-model"] == Claim(
            "Native ability", efforts=("low", "xhigh")
        )
        assert all(route.claim.efforts == ("low", "xhigh") for route in tentacle.routes)
        await tentacle.run(
            "hello",
            conversation_address=KEY,
            thread_id=uuid7(),
            model="first:future-model",
            effort="low",
        )

    [selected] = [
        payload
        for method, payload in FakeDeepseekApi.calls
        if method == "session.selectModel"
    ]
    assert selected == {
        "sessionId": "sess-1",
        "provider": "first",
        "model": "future-model",
        "reasoningEffort": "low",
    }


async def test_deepseek_reports_catalog_failure_without_inventing_models(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset()
    FakeDeepseekApi.results["llm.models"] = OkResult(
        value={
            "groups": [],
            "failures": [
                {"id": "broken", "name": "Broken", "message": "Credentials missing"}
            ],
        }
    )
    tentacle = DeepseekTentacle("deepseek", Octomate(), config=DeepseekConfig())
    with pytest.raises(ValueError, match="no available models"):
        async with tentacle:
            pytest.fail("An empty catalog must not start successfully")
    assert tentacle.models == {}
    assert "Credentials missing" in caplog.text


@pytest.fixture(params=["claude", "codex", "deepseek"])
def harness(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    codex_catalog: AsyncMock,
) -> AgentTentacle[str, None]:
    octomate = Octomate(conversations=FakeConversationManager())
    if request.param == "claude":
        client = AsyncMock()
        client.get_server_info.return_value = {
            "account": {"apiProvider": "source"},
            "models": [
                {"value": name, "displayName": name}
                for name in ("included", "excluded")
            ],
        }
        client.__aenter__.return_value = client
        monkeypatch.setattr(
            claude_base, "ClaudeSDKClient", MagicMock(return_value=client)
        )
        return ClaudeCodeTentacle("claude", octomate, config=ClaudeCodeConfig())
    if request.param == "codex":
        codex_catalog.request.side_effect = [
            ConfigReadResponse.model_validate(
                {
                    "config": {"model_provider": "source", "model": "excluded"},
                    "origins": {},
                }
            ),
            ModelListResponse(
                data=[codex_model("included"), codex_model("excluded", default=True)]
            ),
        ]
        return CodexTentacle("codex", octomate, config=CodexConfig())
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset()
    FakeDeepseekApi.results["host.describe"] = OkResult(
        value={"provider": "source", "model": "excluded"}
    )
    FakeDeepseekApi.results["llm.models"] = OkResult(
        value={
            "groups": [
                {
                    "id": "source",
                    "name": "Source",
                    "models": [
                        {"id": name, "name": name} for name in ("included", "excluded")
                    ],
                }
            ],
            "failures": [],
        }
    )
    return DeepseekTentacle("deepseek", octomate, config=DeepseekConfig())


async def test_harness_catalogs_expose_all_models_and_native_defaults(
    harness: AgentTentacle[str, None],
) -> None:
    tentacle = harness
    assert tentacle.routes == []
    async with tentacle:
        names = ["source:included", "source:excluded"]
        assert list(tentacle.models) == names
        assert list(tentacle.claims) == names
        assert [route.model for route in tentacle.routes] == names
        assert tentacle.routes is tentacle.routes
        assert tentacle.default_model is None
        tentacle.octomate.connect(tentacle)
        channel = TrunklineTentacle(
            "console",
            tentacle.octomate,
            config=TrunklineChannelConfig(agents=[tentacle.id]),
        )
        assert [route.model for route in channel.routable_agents()] == [None, *names]
    assert tentacle.routes == []


async def test_inkling_uses_the_shared_agent_lifecycle() -> None:
    claim = Claim("Test agent", efforts=("medium",))
    tentacle = InklingTentacle(
        "inkling", Octomate(), models={"test": TestModel()}, claims={"test": claim}
    )
    async with tentacle as entered:
        assert entered is tentacle
        routes = tentacle.routes
        assert len(routes) == 1
        assert routes[0].claim is claim
        assert tentacle.routes is routes
    assert tentacle.routes == []
