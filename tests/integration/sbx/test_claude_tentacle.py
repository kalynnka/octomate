"""Opt-in model calls: the Claude tentacle drives a guest CLI in the owner's sandbox."""

import os

import pytest
from pydantic_ai import AgentRunResultEvent
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.agents import ClaudeCodeConfig
from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.habitat import InvocationScope
from octomate.tentacles.claude import ClaudeTentacle
from tests.support.invocations import invocation_for
from tests.support.managers import a_thread
from tests.support.users import a_user

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
    host = Octomate()
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
    owner = await a_user()
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
            if proof is not None:
                # The guest wrote in its working directory; the host reads the
                # same file in the owner's tree.
                written = (invocation.workspace.path / "proof.txt").read_text()
                assert written.strip() == proof
                config = invocation.workspace.volumes.get("config")
                assert config is not None
                sessions = config.path / ".claude" / "projects"
                assert list(sessions.glob("*/*.jsonl")), "No guest session file"
        return str(results[-1].result.output)

    sandbox = host.habitats.residence(
        InvocationScope(user_id=owner.id, conversation_id=uuid7())
    ).habitat
    try:
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
    finally:
        try:
            await host.habitats.close()
        finally:
            if isinstance(sandbox, SbxHabitat) and sandbox.managed:
                await sandbox.command("rm", "--force", sandbox.name)
