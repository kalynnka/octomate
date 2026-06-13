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
from typing import TYPE_CHECKING, Literal, TypeAlias, cast
from uuid import uuid4

from pydantic_ai import AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.output import OutputSpec
from pydantic_core import to_jsonable_python

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
octomate_package = types.ModuleType("octomate")
octomate_package.__path__ = [str(REPO_ROOT / "octomate")]
sys.modules.setdefault("octomate", octomate_package)

from octomate.config import OctomateConfig
from octomate.managers.conversations import ConversationManager
from octomate.providers import ProviderRegistry
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.segments import ImageSegment, OutputSegment
from octomate.tentacles.agent.inkling import (
    build_inkling_agent,
)
from octomate.tentacles.agent.inkling.base import (
    InklingOutput,
)
from octomate.types.json import JsonObject

if TYPE_CHECKING:
    from octomate.capabilities.agent import Agent


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
) -> dict[str, int]:
    config = OctomateConfig()
    conversations = ConversationManager()
    registry = ProviderRegistry(config.providers)
    agent = build_inkling_agent(registry, config.agents.inkling.model)

    output_dir.mkdir(parents=True, exist_ok=True)
    case_counts: dict[str, int] = {}
    with sqlalchemy_materia():
        for case in cases:
            output = output_dir / case.file_name
            with output.open("w", encoding="utf-8") as file:
                case_counts[case.name] = await capture_case(
                    file=file,
                    conversations=conversations,
                    agent=agent,
                    case=case,
                    run_name=run_name,
                    chat_id=chat_id,
                    user_id=user_id,
                    thread_id=thread_id,
                )
    return case_counts


async def capture_case(
    *,
    file: TextIOBase,
    conversations: ConversationManager,
    agent: Agent[None, InklingOutput],
    case: CaptureCase,
    run_name: str,
    chat_id: str,
    user_id: str,
    thread_id: str,
) -> int:
    print(f"capture case: {case.name}")
    effective_chat_id = (
        f"{chat_id}-{case.name}" if chat_id else f"capture-{case.name}-{uuid4().hex}"
    )
    key = ConversationKey(
        channel_tentacle_id="capture",
        chat_type="private",
        chat_id=effective_chat_id,
        user_id=user_id,
        thread_id=thread_id,
    )

    count = 0
    conversation = await conversations.ensure(key, agent_tentacle_id="inkling")
    message_history = cast("list[ModelMessage]", list(conversation.messages))
    async with agent.iter(
        case.prompt,
        output_type=case.output_type,
        message_history=message_history or None,
        conversation_id=str(conversation.id),
    ) as run:
        async for node in run:
            if agent.is_model_request_node(node) or agent.is_call_tools_node(node):
                async with node.stream(run.ctx) as stream:
                    async for event in stream:
                        count = write_event(file, count, event)
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
        await conversations.record_agent_run(
            conversation,
            run_id=run.result.run_id,
            messages=run.result.new_messages(),
            name=f"{run_name}:{case.name}",
        )
    return count


def main() -> None:
    args = parser().parse_args()
    if args.prompt is None:
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
                output_type=list[OutputSegment],
                expectation="segments_with_image",
                required_image=DEFAULT_IMAGE.as_posix(),
            ),
        ]
    else:
        cases = [
            CaptureCase(
                name="plain_text",
                file_name="inkling_text.jsonl",
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
        )
    )
    total = sum(case_counts.values())
    summary = ", ".join(
        f"{name}={count}" for name, count in case_counts.items()
    )
    print(f"captured {total} events ({summary}) -> {args.output_dir}")


if __name__ == "__main__":
    main()
