"""Inkling's project tools and approval resumes use the prepared habitat."""

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import Invocation
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.thread import ThreadKey
from octomate.schemas.volumes import Volume
from octomate.schemas.volumes.storage import HostDirectory
from octomate.schemas.workspaces import Workspace
from octomate.tentacles.inkling import InklingTentacle
from octomate.tentacles.inkling.agent import Agent
from octomate.tentacles.inkling.base import InklingOutput
from tests.agent.claude.test_habitat import executable as executable
from tests.support.managers import a_project, a_registry
from tests.support.users import a_user
from tests.test_habitat_contracts import IMAGE


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("write_file", '{"path":"proof.txt","content":"habitat-only"}'),
        ("run_command", '{"command":"printf habitat-only > proof.txt; pwd"}'),
    ],
)
async def test_approved_project_tools_run_in_the_habitat(
    in_memory_engine: AsyncEngine,
    executable: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: str,
) -> None:
    core = tmp_path / "core"
    core.mkdir()
    (core / "AGENTS.md").write_text("CORE INSTRUCTIONS MUST NOT LOAD")
    guest = tmp_path / "guest"
    guest.mkdir()
    (guest / "AGENTS.md").write_text("GUEST PROJECT INSTRUCTIONS")
    monkeypatch.setattr(SbxHabitat, "path", lambda self, volume: PurePosixPath(guest))
    app = Octomate()
    owner = await a_user()
    project = a_project(core)
    app.workspaces.projects = await a_registry(project)
    thread = await app.threads.ensure(
        ThreadKey("im", "thread", "alice", "work"), project=project
    )
    conversation = await app.conversations.ensure(
        thread.id, agent_tentacle_id="inkling"
    )
    invocation = Invocation(
        id=uuid7(),
        conversation=conversation,
        workspace=Workspace(
            thread_id=thread.id,
            project=project,
            volumes={"work": Volume(owner_id=owner.id, storage=HostDirectory(core))},
        ),
        habitat=SbxHabitat(
            config=SbxHabitatConfig(image=IMAGE, executable=str(executable)),
            user_id=owner.id,
            root=tmp_path,
            config_dir=tmp_path,
        ),
    )
    returned: list[ToolReturnPart] = []

    async def respond(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        assert "GUEST PROJECT INSTRUCTIONS" in (info.instructions or "")
        assert "CORE INSTRUCTIONS MUST NOT LOAD" not in (info.instructions or "")
        returned.extend(
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        )
        if returned:
            yield "done"
        else:
            yield {
                0: DeltaToolCall(name=tool, json_args=arguments, tool_call_id="work")
            }

    agent: Agent[None, InklingOutput] = Agent(
        FunctionModel(stream_function=respond, model_name="scripted"),
        deps_type=type(None),
        output_type=[str, DeferredToolRequests],
    )
    tentacle = InklingTentacle(
        "inkling", app, auth=app.auth, habitats=app.habitats, agent=agent
    )
    address = ChannelAddress(
        channel_tentacle_id="im", chat_type="dm", chat_id="alice", user_id="alice"
    )
    result = await tentacle.run(
        "write proof", conversation_address=address, invocation=invocation
    )
    assert isinstance(result.output, DeferredToolRequests)
    assert [call.tool_name for call in result.output.approvals] == [tool]
    assert not (guest / "proof.txt").exists()

    saved = await tentacle.resolve_conversation(
        thread.id, conversation_address=address, conversation_id=conversation.id
    )
    invocation = replace(invocation, conversation=saved)
    result = await tentacle.run(
        conversation_address=address,
        invocation=invocation,
        deferred_tool_results=DeferredToolResults(approvals={"work": True}),
    )
    assert result.output == "done"
    assert (guest / "proof.txt").read_text() == "habitat-only"
    assert not (core / "proof.txt").exists()
    if tool == "run_command":
        assert str(guest) in returned[-1].model_response_str()
    saved = await app.conversations.get(conversation.id)
    assert saved is not None
    assert all(run.cwd == guest for run in saved.runs)
    assert result.response.workspace_ref is not None
    assert result.response.workspace_ref.provider == "octomate"
