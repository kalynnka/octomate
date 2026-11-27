"""Inkling's project tools and approval resumes use the prepared habitat."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path, PurePosixPath
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.messages import ModelMessage, PartStartEvent, TextPart, ToolReturnPart
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.habitat import SbxHabitatConfig, SrtHabitatConfig
from octomate.habitat import HostedHabitat, ManagedHabitat
from octomate.habitat.sbx import SbxHabitat
from octomate.habitat.srt import SrtHabitat
from octomate.managers.habitat import Invocation
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.thread import ThreadKey
from octomate.schemas.volumes import Volume
from octomate.schemas.volumes.storage import HostDirectory
from octomate.schemas.workspaces import Workspace
from octomate.tentacles.inkling import InklingTentacle
from octomate.tentacles.inkling.agent import Agent
from octomate.tentacles.inkling.base import InklingOutput
from octomate.tentacles.inkling.react import (
    ReactDeps,
    ReactState,
    ResumeTurn,
    StartTurn,
)
from octomate.tentacles.inkling.workspace import HabitatWorkspace
from octomate.tentacles.stream import ReactStreamEvent
from tests.habitat.agents.test_claude import executable as executable
from tests.habitat.test_contracts import IMAGE
from tests.support.managers import a_project, a_registry


@pytest.fixture
async def inkling_habitat(
    executable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[ManagedHabitat]:
    monkeypatch.setattr(
        SbxHabitat, "path", lambda self, volume: PurePosixPath(tmp_path / "guest")
    )
    habitat = SbxHabitat(
        config=SbxHabitatConfig(image=IMAGE, executable=str(executable)),
        user_id=uuid7(),
        root=tmp_path,
        config_dir=tmp_path,
    )
    try:
        yield habitat
    finally:
        await habitat.stop()


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("write_file", '{"path":"proof.txt","content":"habitat-only"}'),
        ("run_command", '{"command":"printf habitat-only > proof.txt; pwd"}'),
    ],
)
async def test_approved_project_tools_run_in_the_habitat(
    in_memory_engine: AsyncEngine,
    inkling_habitat: ManagedHabitat,
    tmp_path: Path,
    tool: str,
    arguments: str,
) -> None:
    core = tmp_path / "core"
    core.mkdir()
    (core / "AGENTS.md").write_text("CORE INSTRUCTIONS MUST NOT LOAD")
    guest = tmp_path / "guest"
    guest.mkdir()
    (guest / "AGENTS.md").write_text("GUEST PROJECT INSTRUCTIONS")
    app = Octomate()
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
            volumes={
                "work": Volume(
                    owner_id=inkling_habitat.user_id,
                    storage=HostDirectory(
                        core if isinstance(inkling_habitat, HostedHabitat) else guest
                    ),
                ),
                "config": Volume(
                    owner_id=inkling_habitat.user_id, storage=HostDirectory(core)
                ),
                "cache": Volume(
                    owner_id=inkling_habitat.user_id, storage=HostDirectory(core)
                ),
            },
        ),
        habitat=inkling_habitat,
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
        "inkling", app, auth=app.auth, habitats=app.habitats["native"], agent=agent
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
    reference = result.response.workspace_ref
    assert invocation.habitat is inkling_habitat

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
    assert invocation.habitat is inkling_habitat
    assert result.response.workspace_ref == reference
    assert result.response.workspace_ref is not None
    assert result.response.workspace_ref.provider == "octomate"


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_workspace_scope_closes_when_the_graph_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: type[RuntimeError] | type[asyncio.CancelledError],
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    owner = SrtHabitat(config=SrtHabitatConfig(), user_id=uuid7(), root=tmp_path)
    scopes: list[SrtHabitat] = []

    async def prepare(self: SrtHabitat) -> None:
        scopes.append(self)

    stop = AsyncMock()
    monkeypatch.setattr(SrtHabitat, "prepare", prepare)
    monkeypatch.setattr(SrtHabitat, "stop", stop)

    async def fail(
        start_node: StartTurn[InklingOutput, None] | ResumeTurn[InklingOutput, None],
        *,
        state: ReactState,
        deps: ReactDeps[InklingOutput, None],
    ) -> AsyncIterator[ReactStreamEvent[InklingOutput]]:
        assert deps.capabilities is not None
        capability = deps.capabilities[0]
        assert isinstance(capability, HabitatWorkspace)
        assert capability.invocation.habitat is scopes[0]
        assert scopes[0].root == work.resolve()
        yield PartStartEvent(index=0, part=TextPart("started"))
        raise error("graph failed")

    monkeypatch.setattr("octomate.tentacles.inkling.base.iter_react_graph_events", fail)
    app = Octomate()
    agent: Agent[None, InklingOutput] = Agent(
        TestModel(), deps_type=type(None), output_type=[str, DeferredToolRequests]
    )
    tentacle = InklingTentacle(
        "inkling", app, auth=app.auth, habitats=app.habitats["native"], agent=agent
    )
    thread = uuid7()
    invocation = Invocation(
        id=uuid7(),
        conversation=Conversation(thread_id=thread, agent_tentacle_id="inkling"),
        workspace=Workspace(
            thread_id=thread,
            project=a_project(work),
            volumes={
                "work": Volume(owner_id=owner.user_id, storage=HostDirectory(work))
            },
        ),
        habitat=owner,
    )
    with pytest.raises(error, match="graph failed"):
        async for _event in tentacle.iter_graph_events(
            user_prompt=None,
            conversation_address=ChannelAddress(
                channel_tentacle_id="im",
                chat_type="dm",
                chat_id="alice",
                user_id="alice",
            ),
            invocation=invocation,
        ):
            pass
    stop.assert_awaited_once()
    assert len(scopes) == 1
    assert scopes[0].scratch is not None
    assert not scopes[0].scratch.exists()
    assert invocation.habitat is owner
