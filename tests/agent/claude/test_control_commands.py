"""Claude receives fixed slash commands in the configured habitat."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.auth import AuthManager
from octomate.schemas.commands import (
    CommandContext,
    CommandError,
    CommandIntent,
    CommandResult,
)
from octomate.schemas.segments import TextSegment
from octomate.tentacles.claude import ClaudeTentacle
from octomate.tentacles.claude.transport import HabitatTransport
from octomate.types.json import JsonObject
from tests.agent.claude.test_command_execution import (
    execution as execution,
)
from tests.agent.claude.test_command_execution import (
    fixture as fixture,
)
from tests.agent.claude.test_command_execution import (
    native_messages,
)
from tests.agent.claude.test_command_execution import (
    streams as streams,
)
from tests.support.users import auth_config
from tests.test_habitat_contracts import IMAGE


async def deliver(
    agent: ClaudeTentacle,
    context: CommandContext,
    command: str,
    arguments: str = "",
) -> CommandResult | CommandError:
    intent = CommandIntent(command_id=command, arguments=arguments)
    async with agent.commands.validate(
        agent, context, intent, delivery_id="control"
    ) as validated:
        if isinstance(validated, CommandResult | CommandError):
            return validated
        async with agent.commands.execute(
            agent, context, intent, validated, delivery_id="control"
        ) as result:
            assert isinstance(result, CommandResult | CommandError)
            return result


@pytest.mark.parametrize(("command", "arguments"), [("plan", "off"), ("fork", "topic")])
async def test_fixed_command_reaches_the_configured_habitat(
    execution: tuple[
        ClaudeTentacle, CommandContext, AsyncMock, list[ClaudeAgentOptions]
    ],
    streams: dict[str, list[JsonObject]],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    arguments: str,
) -> None:
    agent, context, client, host_options = execution
    agent.habitats.config = SbxHabitatConfig(image=IMAGE)
    agent.auth = AuthManager(auth_config())
    monkeypatch.setattr(SbxHabitat, "prepare", AsyncMock())
    monkeypatch.setattr(SbxHabitat, "stop", AsyncMock())
    transport = MagicMock(return_value=client)
    monkeypatch.setattr(HabitatTransport, "client", transport)
    result = {**streams["compact_empty"][-1], "result": "Reply from Claude"}
    client.receive_response.side_effect = lambda: native_messages([result])
    try:
        outcome = await deliver(agent, context, command, arguments)
    finally:
        await agent.revoke_runtime_keys()
        await agent.habitats.close()
    transport.assert_called_once()
    habitat, _ = transport.call_args.args
    assert isinstance(habitat, SbxHabitat)
    assert habitat.user_id == context.user_id
    assert not host_options
    client.query.assert_awaited_once_with(f"/{command} {arguments}")
    assert isinstance(outcome, CommandResult)
    assert outcome.segments == [TextSegment(data={"text": "Reply from Claude"})]
    assert len(await agent.threads.list_threads(user_id=context.user_id)) == 1
