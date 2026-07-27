"""Capture a raw Inkling Pydantic AI streamed run as JSONL.

Run from the repo root with the same config/env as the app:

    uv run python scripts/capture_inkling_events.py

The default output lands in tests/src/events so tests can replay the raw event shape
through Octomate's stream wrappers and channel feelers later.
"""

# ruff: noqa: E402  -- octomate imports run after the sys.path bootstrap below.
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import types
from dataclasses import dataclass
from io import TextIOBase
from pathlib import Path
from typing import Literal, TypeAlias, cast
from uuid import uuid4

from pydantic_ai import AgentCapability, AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
)
from pydantic_ai.output import OutputSpec
from pydantic_core import to_jsonable_python

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
octomate_package = types.ModuleType("octomate")
octomate_package.__path__ = [str(REPO_ROOT / "octomate")]
sys.modules.setdefault("octomate", octomate_package)

from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import AbstractToolset

from octomate.base import Octomate
from octomate.capabilities.harness.agent import Agent
from octomate.capabilities.gateway import SCHEME_TOOL_NAME, GatewayCapability
from octomate.capabilities.send import SendCapability
from octomate.capabilities.todos import TodoCapability
from octomate.config import OctomateConfig
from octomate.config.agents import AgentRouteModelName
from octomate.managers.conversation import ConversationManager
from octomate.providers import ProviderRegistry
from octomate.schemas.base import sqlalchemy_materia
from octomate.managers.thread import ThreadManager
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import ImageSegment, MessageSegment
from octomate.schemas.triage import AgentRoute, Claim
from octomate.capabilities.ask import AskCapability
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.agent.inkling import build_mcp_toolsets
from octomate.tentacles.agent.inkling.base import (
    InklingOutput,
    InklingTentacle,
)
from octomate.tentacles.agent.inkling.prompts import SYSTEM_PROMPT
from octomate.types.json import JsonObject

DEFAULT_PROMPT = (
    "Use your todo tools to create a two-step plan for checking Octomate stream "
    "capture, mark work in progress, complete the tasks, and then give a concise "
    "final answer mentioning begin, thinking, tool calls, and streamed results. "
    "Do not ask questions."
)
SEGMENTS_PROMPT = (
    "Return a structured segment response for stream capture. Include at least "
    "one markdown or text segment and exactly one image segment. Use this exact "
    "local image file path in the image segment: tests/src/images/usagi.jpg. "
    "Keep the message concise and do not ask questions."
)
SUBAGENTS_PROMPT = (
    "This is a subagent capture test. First call scry. Then, in one assistant "
    "turn, call scheme twice using the route scry returns. Name the accomplices "
    "timeline-contract and failure-review. Ask timeline-contract for three "
    "invariants of an independent subagent activity timeline. Ask failure-review "
    "for three failure cases whose partial report should remain reviewable. After "
    "both reports return, give a concise synthesis. Do not summon, teleport, "
    "whisper, or do the delegated work yourself."
)
DEFAULT_OUTPUT_DIR = Path("tests/src/events")
DEFAULT_IMAGE = Path("tests/src/images/usagi.jpg")
RawCapturedEvent: TypeAlias = AgentStreamEvent | AgentRunResultEvent[InklingOutput]
Expectation: TypeAlias = Literal["plain_text", "segments_with_image"]


@dataclass(frozen=True)
class CaptureCase:
    name: str
    file_name: str
    prompt: str
    output_type: OutputSpec[InklingOutput]
    expectation: Expectation
    required_image: str | None = None
    subagents: bool = False


def event_payload(event: RawCapturedEvent) -> JsonObject:
    return cast(JsonObject, to_jsonable_python(event))


def write_event(file: TextIOBase, count: int, event: RawCapturedEvent) -> int:
    count += 1
    payload = event_payload(event)
    file.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    file.write("\n")
    print(f"{count:04d} {type(event).__name__} {payload.get('event_kind')}")
    return count


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the configured Inkling agent and capture stream events."
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="Optional prompt override. When set, captures one plain-text run.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"JSONL destination directory. Defaults to {DEFAULT_OUTPUT_DIR}.",
    )
    parser.add_argument(
        "--file-name",
        default=None,
        help="Output file for a prompt override or subagent run.",
    )
    parser.add_argument(
        "--subagents",
        action="store_true",
        help="Capture one real Inkling run that completes two scheme calls.",
    )
    parser.add_argument(
        "--mcp",
        action="store_true",
        help="Attach the operator MCP toolsets configured under `mcp`.",
    )
    parser.add_argument(
        "--run-name",
        default="capture",
        help="Run name persisted with the conversation history.",
    )
    parser.add_argument(
        "--chat-id",
        default="",
        help="Conversation chat id used for this capture run. Defaults to a fresh id.",
    )
    parser.add_argument(
        "--user-id",
        default="local",
        help="Conversation user id used for this capture run.",
    )
    parser.add_argument(
        "--thread-id",
        default="",
        help="Optional conversation thread id used for this capture run.",
    )
    return parser


async def capture(
    *,
    cases: list[CaptureCase],
    output_dir: Path,
    run_name: str,
    chat_id: str,
    user_id: str,
    thread_id: str,
    with_mcp: bool = False,
) -> dict[str, int]:
    config = OctomateConfig()
    conversations = ConversationManager()
    threads = ThreadManager()
    registry = ProviderRegistry(config.providers)
    toolsets: list[AbstractToolset[None]] = []
    if with_mcp:
        toolsets = build_mcp_toolsets(config.mcp)
    model_config = config.agents.inkling.default_model
    agent: Agent[None, InklingOutput] = Agent(
        registry.build_model(model_config),
        deps_type=type(None),
        name="octomate-inkling",
        output_type=[str, list[MessageSegment], DeferredToolRequests],
        toolsets=toolsets,
        capabilities=[AskCapability(), TodoCapability(), SendCapability()],
        system_prompt=SYSTEM_PROMPT,
    )
    accomplice: InklingTentacle | None = None
    if any(case.subagents for case in cases):
        host = Octomate(thread_manager=threads, conversations=conversations)
        accomplice = InklingTentacle(
            "capture-accomplice",
            host,
            models={model_config.name: registry.build_model(model_config)},
            conversation_manager=conversations,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    case_counts: dict[str, int] = {}
    with sqlalchemy_materia():
        for case in cases:
            output = output_dir / case.file_name
            with output.open("w", encoding="utf-8") as file:
                case_counts[case.name] = await capture_case(
                    file=file,
                    conversations=conversations,
                    threads=threads,
                    agent=agent,
                    case=case,
                    run_name=run_name,
                    chat_id=chat_id,
                    user_id=user_id,
                    thread_id=thread_id,
                    accomplice=accomplice,
                    subagent_model=model_config.name,
                )
    return case_counts


async def capture_case(
    *,
    file: TextIOBase,
    conversations: ConversationManager,
    threads: ThreadManager,
    agent: Agent[None, InklingOutput],
    case: CaptureCase,
    run_name: str,
    chat_id: str,
    user_id: str,
    thread_id: str,
    accomplice: InklingTentacle | None,
    subagent_model: AgentRouteModelName,
) -> int:
    print(f"capture case: {case.name}")
    effective_chat_id = (
        f"{chat_id}-{case.name}" if chat_id else f"capture-{case.name}-{uuid4().hex}"
    )
    address = ChannelAddress(
        channel_tentacle_id="capture",
        chat_type="private",
        chat_id=effective_chat_id,
        user_id=user_id,
        thread_id=thread_id,
    )

    count = 0
    thread = await threads.ensure(address)
    conversation = await conversations.ensure(thread.id, agent_tentacle_id="inkling")
    message_history = cast("list[ModelMessage]", list(conversation.messages))
    capabilities: list[AgentCapability[None]] = []
    if case.subagents:
        if accomplice is None:
            raise RuntimeError("subagent capture requires an accomplice")
        agents: dict[str, AgentTentacle] = {accomplice.id: accomplice}
        capabilities.append(
            GatewayCapability(
                routes=[
                    AgentRoute(
                        agent_id=accomplice.id,
                        model=subagent_model,
                        claim=Claim(ability="independent analysis for capture tests"),
                    )
                ],
                current_agent_id="inkling",
                agents=agents,
                conversations=conversations,
                thread_id=thread.id,
                conversation_address=address,
            )
        )
    scheme_calls = 0
    scheme_results = 0
    async with agent.iter(
        case.prompt,
        output_type=case.output_type,
        message_history=message_history or None,
        conversation_id=str(conversation.id),
        capabilities=capabilities or None,
    ) as run:
        async for node in run:
            if agent.is_model_request_node(node) or agent.is_call_tools_node(node):
                async with node.stream(run.ctx) as stream:
                    async for event in stream:
                        count = write_event(file, count, event)
                        if (
                            isinstance(event, FunctionToolCallEvent)
                            and event.part.tool_name == SCHEME_TOOL_NAME
                        ):
                            scheme_calls += 1
                        elif (
                            isinstance(event, FunctionToolResultEvent)
                            and event.part.tool_name == SCHEME_TOOL_NAME
                        ):
                            scheme_results += 1
        if run.result is None:
            raise RuntimeError(f"{case.name} capture completed without a run result")

        output = run.result.output
        if case.expectation == "plain_text":
            if not isinstance(output, str):
                raise RuntimeError(
                    f"{case.name} expected str output, got {type(output)}"
                )
        else:
            if not isinstance(output, list):
                raise RuntimeError(
                    f"{case.name} expected segment output, got {type(output)}"
                )
            if case.required_image is None:
                raise RuntimeError(f"{case.name} expected an image path requirement")
            images = [
                segment
                for segment in output
                if isinstance(segment, ImageSegment)
                and segment.data.file == case.required_image
            ]
            if not images:
                raise RuntimeError(
                    f"{case.name} expected image segment with {case.required_image}"
                )

        result_event = AgentRunResultEvent(run.result)
        count = write_event(file, count, result_event)
        if case.subagents and (scheme_calls < 2 or scheme_results < 2):
            raise RuntimeError(
                f"subagent capture expected two scheme pairs, got "
                f"calls={scheme_calls}, results={scheme_results}"
            )
        await conversations.record_agent_run(
            conversation,
            run_id=run.result.run_id,
            messages=run.result.new_messages(),
            name=f"{run_name}:{case.name}",
        )
    return count


def main() -> None:
    args = parser().parse_args()
    if args.subagents:
        cases = [
            CaptureCase(
                name="subagents",
                file_name=args.file_name or "inkling_subagents.jsonl",
                prompt=args.prompt or SUBAGENTS_PROMPT,
                output_type=str,
                expectation="plain_text",
                subagents=True,
            )
        ]
    elif args.prompt is None:
        if not DEFAULT_IMAGE.exists():
            raise FileNotFoundError(DEFAULT_IMAGE)
        cases = [
            CaptureCase(
                name="plain_text",
                file_name="inkling_text.jsonl",
                prompt=DEFAULT_PROMPT,
                output_type=str,
                expectation="plain_text",
            ),
            CaptureCase(
                name="segments_with_image",
                file_name="inkling_segments.jsonl",
                prompt=SEGMENTS_PROMPT,
                output_type=list[MessageSegment],
                expectation="segments_with_image",
                required_image=DEFAULT_IMAGE.as_posix(),
            ),
        ]
    else:
        cases = [
            CaptureCase(
                name="plain_text",
                file_name=args.file_name or "inkling_text.jsonl",
                prompt=args.prompt,
                output_type=str,
                expectation="plain_text",
            )
        ]
    case_counts = asyncio.run(
        capture(
            cases=cases,
            output_dir=args.output_dir,
            run_name=args.run_name,
            chat_id=args.chat_id,
            user_id=args.user_id,
            thread_id=args.thread_id,
            with_mcp=args.mcp,
        )
    )
    total = sum(case_counts.values())
    summary = ", ".join(f"{name}={count}" for name, count in case_counts.items())
    print(f"captured {total} events ({summary}) -> {args.output_dir}")


if __name__ == "__main__":
    main()
