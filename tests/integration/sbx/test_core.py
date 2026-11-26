"""Claude's guest hook retains a transcript without a core upload execution."""

import json
import os
from contextlib import AsyncExitStack, suppress
from pathlib import Path

import anyio
import pytest
from pydantic import HttpUrl
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.habitat.sbx import SbxHabitat
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.habitat import InvocationScope
from octomate.tentacles.claude import ClaudeTentacle
from tests.agent.claude.test_driven_transcript import (
    FIRST,
    SECOND,
    a_driven_turn,
    kept_bytes,
    served,
)
from tests.integration.sbx import remove_sandbox
from tests.support.invocations import invocation_for


async def upload_subagent(
    sandbox: SbxHabitat, conversation: Conversation, path: Path, child: Path, token: str
) -> None:
    """Deliver SubagentStop through the actual CLI inside the VM."""
    async with sandbox.resident(
        "octomate",
        "claude",
        "upload",
        "--conversation",
        str(conversation.id),
        workdir="/home/agent/workspace",
        env={
            "OCTOMATE_CLI_URL": sandbox.core_url,
            "OCTOMATE_CLI_TOKEN": token,
        },
    ) as stream:
        await stream.send(
            json.dumps(
                {
                    "hook_event_name": "SubagentStop",
                    "session_id": conversation.external_id,
                    "transcript_path": str(path),
                    "agent_id": "proof",
                    "agent_transcript_path": str(child),
                }
            ).encode()
        )
        await stream.send_eof()
        with suppress(anyio.EndOfStream):
            assert await stream.receive() == b""


@pytest.mark.timeout(300)
async def test_guest_claude_hook_uploads_before_the_invocation_is_released(
    in_memory_engine: AsyncEngine,
    sbx_cleanup: AsyncExitStack,
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
        sbx_cleanup.push_async_callback(remove_sandbox, sandbox)
        sbx_cleanup.push_async_callback(octomate.habitats.close)
        sbx_cleanup.push_async_callback(tentacle.revoke_runtime_keys)
        address = ChannelAddress(
            channel_tentacle_id="im", chat_type="dm", chat_id="lu", user_id="lu"
        )
        previous = b""
        child_bytes = b""
        for turn in range(2):
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
                child_path = path.with_suffix("") / "subagents" / "agent-proof.jsonl"
                child_path.parent.mkdir(parents=True, exist_ok=True)
                child_bytes += FIRST if turn == 0 else SECOND
                child_path.write_bytes(child_bytes)
                key = await tentacle.runtime_api_key(user.id)
                assert key is not None
                await upload_subagent(
                    sandbox, stored, path, child_path, key.token.get_secret_value()
                )
                [child] = await octomate.conversations.subagents(conversation.id)
                assert child.subagent_id == "proof"
                assert await kept_bytes(octomate, child, user) == child_bytes
                assert child.transcript_file_id != stored.transcript_file_id
                assert await kept_bytes(octomate, conversation, user) == retained
