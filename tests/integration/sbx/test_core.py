"""Claude's guest hook retains a transcript without a core upload execution."""

import os

import pytest
from pydantic import HttpUrl
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.habitat.sbx import SbxHabitat
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.habitat import InvocationScope
from octomate.tentacles.claude import ClaudeTentacle
from tests.agent.claude.test_driven_transcript import (
    a_driven_turn,
    kept_bytes,
    served,
)
from tests.support.invocations import invocation_for


@pytest.mark.timeout(300)
async def test_guest_claude_hook_uploads_before_the_invocation_is_released(
    in_memory_engine: AsyncEngine,
) -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    if not image:
        pytest.fail("Set SBX_TEST_IMAGE to the immutable SDK image reference")
    async with (
        a_driven_turn(image=image) as (_, octomate, conversation, user),
        served(octomate) as base,
    ):
        tentacle = octomate.tentacles["claude"]
        assert isinstance(tentacle, ClaudeTentacle)
        scope = InvocationScope(user_id=user.id, conversation_id=conversation.id)
        sandbox = octomate.habitats.residence(scope).habitat
        assert isinstance(sandbox, SbxHabitat)
        sandbox.config = sandbox.config.model_copy(
            update={"core_address": HttpUrl(base)}
        )
        try:
            address = ChannelAddress(
                channel_tentacle_id="im", chat_type="dm", chat_id="lu", user_id="lu"
            )
            previous = b""
            for _ in range(2):
                conversation = await octomate.conversations.get(conversation.id)
                async with (
                    invocation_for(
                        tentacle,
                        conversation_address=address,
                        thread_id=conversation.thread_id,
                        conversation=conversation,
                        owner_id=user.id,
                    ) as invocation,
                    tentacle.run_stream_events(
                        "/context",
                        conversation_address=address,
                        invocation=invocation,
                        run_name="react",
                        interactive=False,
                    ) as events,
                ):
                    # /context runs locally in Claude; it makes no model request.
                    async for _event in events:
                        pass
                    config = invocation.workspace.volumes.get("config")
                    assert config is not None
                    stored = await octomate.conversations.get(conversation.id)
                    path = next(
                        (config.path / ".claude" / "projects").glob(
                            f"*/{stored.external_id}.jsonl"
                        )
                    )
                    retained = await kept_bytes(octomate, conversation, user)
                    assert retained == path.read_bytes()
                    assert retained.startswith(previous)
                    assert len(retained) > len(previous)
                    previous = retained
        finally:
            await tentacle.revoke_runtime_keys()
            await octomate.habitats.close()
            names = (await sandbox.command("ls", quiet=True)).decode().splitlines()
            if sandbox.name in names:
                await sandbox.command("rm", "--force", sandbox.name)
