"""Opt-in model calls: the Claude tentacle drives a guest CLI in the owner's sandbox."""

import json
import os

import pytest
from pydantic import HttpUrl
from pydantic_ai import AgentRunResultEvent
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.config.agents import ClaudeCodeConfig
from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.habitat import InvocationScope
from octomate.tentacles.claude import ClaudeTentacle
from tests.agent.claude.test_driven_transcript import kept_bytes, served
from tests.support.invocations import invocation_for
from tests.support.managers import a_thread
from tests.support.users import a_user, auth_config

pytestmark = [
    pytest.mark.timeout(600),
    pytest.mark.skipif(
        os.environ.get("SBX_TEST_CLAUDE_TURN") != "1",
        reason="Set SBX_TEST_CLAUDE_TURN=1 to use the sbx Claude credential for model calls",
    ),
]

ADDRESS = ChannelAddress(
    channel_tentacle_id="im", chat_type="dm", chat_id="alice", user_id="alice"
)


async def test_a_driven_turn_writes_into_the_owners_tree_and_resumes(
    in_memory_engine: AsyncEngine,
) -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    if not image:
        pytest.fail("Set SBX_TEST_IMAGE to the immutable SDK image reference")
    host = Octomate(config=OctomateConfig(auth=auth_config()))
    host.habitats.config = SbxHabitatConfig(image=image)
    tentacle = ClaudeTentacle(
        host,
        auth=host.auth,
        habitats=host.habitats,
        # Edits inside the working directory need no approval, so no user is asked.
        config=ClaudeCodeConfig(permission_mode="acceptEdits", max_turns=4),
        commands=host.commands,
        projects=host.projects,
        threads=host.threads,
        files=host.files,
        conversations=host.conversations,
        deferred_actions=host.deferred_actions,
        workspaces=host.workspaces,
        users=host.users,
        bearers=host.bearers,
        mcp=host.mcp,
    )
    host.connect(tentacle)
    owner = await a_user(profiles={"claude": "lu"})
    thread_id = await a_thread()
    model = os.environ.get("SBX_TEST_CLAUDE_MODEL", "haiku")
    token = uuid7().hex

    async def turn(prompt: str, proof: str | None = None) -> str:
        async with (
            invocation_for(
                tentacle,
                conversation_address=ADDRESS,
                thread_id=thread_id,
                owner_id=owner.id,
            ) as invocation,
            tentacle.run_stream_events(
                prompt,
                conversation_address=ADDRESS,
                invocation=invocation,
                run_name="react",
                model=model,
                interactive=False,
            ) as stream,
        ):
            results = [
                event
                async for event in stream
                if isinstance(event, AgentRunResultEvent)
            ]
            config = invocation.workspace.volumes.get("config")
            assert config is not None
            sessions = config.path / ".claude" / "projects"
            stored = await host.conversations.get(
                invocation.conversation.id, with_runs=False
            )
            transcript = next(sessions.glob(f"*/{stored.external_id}.jsonl"))
            assert await kept_bytes(host, invocation.conversation, owner) == (
                transcript.read_bytes()
            )
            if proof is not None:
                # The guest wrote in its working directory; the host reads the
                # same file in the owner's tree.
                proof_file = invocation.workspace.path / "proof.txt"
                assert proof_file.is_file(), str(results[-1].result.output)
                written = proof_file.read_text()
                assert written.strip() == proof
        return str(results[-1].result.output)

    sandbox = host.habitats.residence(
        InvocationScope(user_id=owner.id, conversation_id=uuid7())
    ).habitat
    try:
        async with served(host) as base:
            sandbox.config = sandbox.config.model_copy(
                update={"core_address": HttpUrl(base)}
            )
            await turn(
                f"Use the Write tool to create proof.txt containing exactly {token}. "
                "Then reply with the single word done.",
                proof=token,
            )
            # Resuming reads the session the guest kept in the configuration volume.
            answer = await turn(
                "What token did you write to proof.txt in the previous turn? "
                "Reply with only that token; do not use tools."
            )
            assert token in answer
            await turn("/compact")
            conversation = await host.conversations.ensure(
                thread_id, agent_tentacle_id=tentacle.id, with_runs=False
            )
            assert conversation.external_id is not None
            [boundary] = await host.conversations.native_compactions(
                conversation, conversation.external_id
            )
            [message] = boundary.messages
            transcript = await kept_bytes(host, conversation, owner)
            [summary] = [
                row
                for row in map(json.loads, transcript.splitlines())
                if row.get("isCompactSummary")
                and row["parentUuid"] == boundary.native_compaction_id
            ]
            assert summary["sessionId"] == conversation.external_id
            assert message.metadata == {"native_item_ids": [summary["uuid"]]}
            assert message.message_text == summary["message"]["content"]
            assert message.run_id is None
    finally:
        try:
            await tentacle.revoke_runtime_keys()
            await host.habitats.close()
        finally:
            if isinstance(sandbox, SbxHabitat) and sandbox.managed:
                await sandbox.command("rm", "--force", sandbox.name)
