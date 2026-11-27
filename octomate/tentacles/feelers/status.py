"""Transient status presentation from the existing reflex and run events."""

from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from random import choice
from typing import ClassVar

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    OutputToolCallEvent,
    OutputToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
)

from octomate.events import (
    ActionBatchEvent,
    HabitatPreparationEvent,
    ReflexAsleepEvent,
    ReflexAwakeEvent,
    ReflexPhaseChangedEvent,
    RunActivity,
    RunActivityChangedEvent,
    RunEndedEvent,
    RunErrorEvent,
    RunStartedEvent,
    SubagentStartedEvent,
    WireEvent,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.feelers.output import SegmentsFeeler, humanize_tool_name

logger = logging.getLogger(__name__)

type StatusUpdate = tuple[ChannelAddress | None, str | None, float]

# Every way to say one thing. A line says what kind of work is going on; the
# timeline says exactly what, so nothing here takes an argument or a result.
PHRASES: dict[str, tuple[str, ...]] = {
    "preparing_habitat": (
        "Preparing habitat",
        "Warming the habitat",
        "Making room for tentacles",
        "Furnishing the tide pool",
        "Getting the burrow ready",
        "Unpacking the tool chest",
    ),
    "preparing": (
        "Waking",
        "Stirring",
        "Surfacing",
        "Unfurling",
        "Blinking",
        "Uncoiling",
    ),
    "routing": (
        "Finding the right agent",
        "Routing",
        "Picking a tentacle",
        "Reading the currents",
        "Navigating",
        "Testing the waters",
    ),
    # A gateway move is said by its spell.
    "summon": ("Summoning",),
    "teleport": ("Teleporting",),
    "scheme": ("Scheming",),
    "moving": ("Moving",),
    "finalizing": (
        "Wrapping up",
        "Tidying up",
        "Settling down",
        "Tucking tentacles",
        "Tying off",
        "Dropping anchor",
        "Coming into port",
    ),
    "picked_up": (
        "Handing to {agent}",
        "Passing to {agent}",
        "Waking {agent}",
        "Calling {agent}",
        "Hailing {agent}",
        "Tapping {agent}",
        "Releasing {agent}",
        "Inking in {agent}",
        "Handing {agent} the helm",
        "Welcoming {agent} aboard",
    ),
    "started": (
        "Starting",
        "Diving in",
        "Jetting in",
        "Casting off",
        "Setting sail",
        "Weighing anchor",
        "Taking the plunge",
    ),
    "working": (
        "Working on it",
        "Working",
        "All arms busy",
        "Octotasking",
        "Making headway",
        "Going swimmingly",
        "Full steam ahead",
    ),
    "thinking": (
        "Thinking",
        "Pondering",
        "Mulling it over",
        "Fathoming",
        "Deepseeking",
        "Brinestorming",
        "Cephalopondering",
        "Plumbing the depths",
        "Consulting nine brains",
        "Sounding it out",
        "Getting to the bottom",
    ),
    "writing": (
        "Writing",
        "Inking",
        "Penning",
        "Drafting",
        "Spilling ink",
    ),
    "compacting": (
        "Compacting",
        "Tidying memory",
        "Squeezing",
        "Packing",
        "Condensing",
        "Trimming the sails",
        "Lightening the load",
    ),
    "using_tools": (
        "Using tools",
        "Reaching for tools",
        "Arming with tools",
        "Tooling up",
    ),
    "juggling": (
        "Juggling {count} tools",
        "Running {count} tools",
        "Spinning {count} tools",
        "Armed with {count} tools",
        "{count} arms busy",
        "Octotasking {count} tools",
    ),
    "read": (
        "Reading",
        "Skimming",
        "Poring over",
        "Getting a feel",
    ),
    "edit": (
        "Editing",
        "Reshaping",
        "Inking in",
        "Reefactoring",
        "Making waves",
        "Patching the hull",
        "Changing colors",
    ),
    "run": (
        "Running a command",
        "Working the shell",
        "Cracking the shell",
        "Shelling out",
        "Taking command",
    ),
    "find": (
        "Searching",
        "Feeling",
        "Combing",
        "Foraging",
        "Probing",
        "Fishing",
        "Dredging",
        "Angling",
    ),
    "web": (
        "Searching the web",
        "Trawling the web",
        "Surfing the web",
        "Netting results",
    ),
    "fetch": (
        "Fetching a page",
        "Fetching",
        "Netting a page",
        "Reeling it in",
        "Landing a page",
    ),
    "plan": (
        "Planning",
        "Replanning",
        "Charting a course",
        "Plotting a course",
    ),
    "delegate": (
        "Briefing a helper",
        "Delegating",
        "Lending an arm",
        "Calling for kelp",
    ),
    "subagent": ("Running {name}",),
    "commission": ("Commissioning {name}",),
    "whisper": ("Whispering with {name}",),
}

# The harnesses' own tools by the kind of work they do. A tool an MCP server
# brought is named with its server instead: its name alone says too little.
TOOL_KINDS: dict[str, str] = {
    "read": "read",
    "notebookread": "read",
    "read_file": "read",
    "write": "edit",
    "edit": "edit",
    "multiedit": "edit",
    "notebookedit": "edit",
    "write_file": "edit",
    "edit_file": "edit",
    "apply_patch": "edit",
    "codex_file_change": "edit",
    "bash": "run",
    "bashoutput": "run",
    "shell": "run",
    "codex_command_execution": "run",
    "grep": "find",
    "glob": "find",
    "websearch": "web",
    "web_search": "web",
    "codex_web_search": "web",
    "webfetch": "fetch",
    "web_fetch": "fetch",
    "todowrite": "plan",
    "write_todos": "plan",
    "task": "delegate",
    "agent": "delegate",
    # Codex reports every MCP and dynamic call under one name that says nothing.
    "codex_mcp_tool_call": "using_tools",
    "codex_dynamic_tool_call": "using_tools",
}


def bound_label(text: str) -> str:
    """A label from outside this module as one printable line a platform can hold."""
    printable = "".join(char for char in text if char.isprintable() or char.isspace())
    return re.sub(r"(?:…+|\.{3,})$", "", " ".join(printable.split())[:120]).rstrip()


@dataclass
class StatusNarrator:
    """One thread's status line, worded from the events of its kick.

    A phrase is drawn when an activity begins and kept while it lasts, so a
    stream of deltas does not reshuffle it and the same work reads differently
    the next time round.
    """

    activity: RunActivity = "working"  # The canonical activity behind the line.
    topic: str = ""  # What the line is about; a new topic draws a new phrase.
    line: str = ""
    tools: dict[str, str] = field(default_factory=dict)  # Unreturned calls, by id.

    def narrate(self, event: WireEvent) -> str | None:
        """The line after `event`: empty to dismiss it, None to leave it alone."""
        match event:
            case HabitatPreparationEvent(phase="preparing"):
                return self.say("preparing_habitat")
            case HabitatPreparationEvent(phase="ready"):
                return self.say("working")
            case ReflexAwakeEvent() | ReflexPhaseChangedEvent(phase="preparing"):
                return self.say("preparing")
            case ReflexPhaseChangedEvent(phase="moving"):
                return self.say(event.spell or "moving")
            case ReflexPhaseChangedEvent():
                return self.say(event.phase)
            case RunStartedEvent(agent_tentacle_id=str() as agent):
                self.tools.clear()
                return self.say("picked_up", agent=agent[:1].upper() + agent[1:])
            case RunStartedEvent():
                self.tools.clear()
                return self.say("started")
            case RunActivityChangedEvent(display_text=str() as text) if (
                label := bound_label(text)
            ):
                return self.hold(label, event.activity)
            case RunActivityChangedEvent(activity="working"):
                self.tools.clear()
                # The run's opening beat does not talk over who picked it up.
                if self.topic.startswith(("started", "picked_up")):
                    return self.line
                return self.say("working")
            case RunActivityChangedEvent(activity="using_tools") if self.tools:
                return self.name_tools()
            case RunActivityChangedEvent():
                return self.say(event.activity, event.activity)
            case (
                PartStartEvent(part=ThinkingPart())
                | PartDeltaEvent(delta=ThinkingPartDelta())
            ):
                return self.say("thinking", "thinking")
            case (
                PartStartEvent(part=TextPart())
                | PartDeltaEvent(delta=TextPartDelta())
                | OutputToolCallEvent()
            ):
                return self.say("writing", "writing")
            case PartStartEvent(part=ToolCallPart(tool_name=tool_name)):
                return self.name_tool(tool_name)
            case FunctionToolCallEvent():
                self.tools[event.part.tool_call_id] = event.part.tool_name
                return self.name_tools()
            case FunctionToolResultEvent():
                # One tool finishing does not mean its parallel siblings finished.
                self.tools.pop(event.tool_call_id, None)
                return self.name_tools() if self.tools else None
            case SubagentStartedEvent():
                return self.say(event.kind, name=event.name)
            case ActionBatchEvent():
                ask = (
                    "approval"
                    if event.approvals and not event.questions
                    else "answer"
                    if event.questions and not event.approvals
                    else "input"
                )
                return self.hold(
                    f"Waiting for your {ask}"
                    if event.blocks_run is True
                    else f"Working · {ask.capitalize()} requested"
                )
            case RunEndedEvent() | ReflexAsleepEvent() | RunErrorEvent():
                self.tools.clear()
                return self.hold("")
            case _:
                return None

    def say(
        self, topic: str, activity: RunActivity = "working", **details: str | int
    ) -> str:
        """Draw a phrase for a topic that just began; repeat it while it lasts."""
        subject = f"{topic} {details}" if details else topic
        if subject == self.topic:
            return self.line
        phrase = choice(PHRASES[topic]).format(**details)
        return self.hold(bound_label(phrase), activity, subject)

    def hold(
        self, line: str, activity: RunActivity = "working", topic: str = ""
    ) -> str:
        """Show a line as given; with no topic, whatever comes next replaces it."""
        self.activity, self.line, self.topic = activity, line, topic
        return line

    def name_tools(self) -> str:
        if len(self.tools) > 1:
            return self.say("juggling", "using_tools", count=len(self.tools))
        (name,) = self.tools.values()
        return self.name_tool(name)

    def name_tool(self, name: str) -> str:
        server, _, tool = name.rpartition("__")
        if not server and (kind := TOOL_KINDS.get(tool.lower())):
            return self.say(kind, "using_tools")
        label = humanize_tool_name(name)
        if server:
            label += f" via {humanize_tool_name(server.removeprefix('mcp__'))}"
        return self.hold(bound_label(label), "using_tools")


class StatusFeeler(ABC):
    """Coalesce status writes per thread, independently of timeline rendering."""

    timeout: ClassVar[float] = 120.0  # Silence dismisses the indicator, not the run.
    refresh_interval: ClassVar[float] = 1.0  # Bound repeated-label platform calls.
    update_interval: ClassVar[float] = 0.0  # Minimum gap between changed labels.

    def __init__(self) -> None:
        self.queues: dict[ThreadKey, asyncio.Queue[StatusUpdate]] = {}
        self.narrators: dict[ThreadKey, StatusNarrator] = {}
        self.tasks: set[asyncio.Task[None]] = set()

    @abstractmethod
    async def present(self, address: ChannelAddress, text: str) -> None:
        """Set the platform's indicator; an empty string dismisses it."""

    def feed(self, address: ChannelAddress, event: WireEvent) -> None:
        """Queue a presentation hint without waiting for platform I/O."""
        key = ThreadKey.model_validate(address)
        narrator = self.narrators.get(key) or StatusNarrator()
        text = narrator.narrate(event)
        if text is None and not isinstance(
            event, PartEndEvent | FunctionToolResultEvent | OutputToolResultEvent
        ):
            return
        if text:
            self.narrators[key] = narrator
            text = f"{text[:119]}…"
        elif text == "":
            self.narrators.pop(key, None)
        queue = self.queues.get(key)
        if queue is None:
            if not text:
                return
            queue = asyncio.Queue()
            self.queues[key] = queue
            task = asyncio.create_task(self.render(key, address, queue))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        queue.put_nowait((address, text, asyncio.get_running_loop().time()))

    async def render(
        self,
        key: ThreadKey,
        address: ChannelAddress,
        queue: asyncio.Queue[StatusUpdate],
    ) -> None:
        """One writer owns a thread's pending hints and inactivity deadline."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        text = ""
        shown: str | None = None
        shown_at = -float("inf")
        try:
            while True:
                wake_at = deadline
                if text and text != shown:
                    wake_at = min(deadline, shown_at + self.update_interval)
                try:
                    target, update, received_at = await asyncio.wait_for(
                        queue.get(), max(0, wake_at - loop.time())
                    )
                    address = target or address
                    deadline = received_at + self.timeout
                except TimeoutError:
                    update = None
                if update is not None:
                    text = update
                while not queue.empty():
                    target, update, received_at = queue.get_nowait()
                    address = target or address
                    text = update if update is not None else text
                    deadline = received_at + self.timeout
                if loop.time() >= deadline:
                    text = ""
                changed = text != shown and (
                    not text or loop.time() - shown_at >= self.update_interval
                )
                refresh = text and loop.time() - shown_at >= self.refresh_interval
                if changed or refresh:
                    await self.display(address, text)
                    shown, shown_at = text, loop.time()
                if not text and queue.empty():
                    return
        finally:
            self.queues.pop(key)
            self.narrators.pop(key, None)

    async def display(self, address: ChannelAddress, text: str) -> None:
        """A failed status hint must not interrupt content or later status delivery."""
        try:
            await self.present(address, text)
        except Exception:
            logger.warning("Status presentation failed for %s", address, exc_info=True)

    async def close(self) -> None:
        """Let in-flight writes settle, then clear each thread last."""
        for queue in self.queues.values():
            queue.put_nowait((None, "", asyncio.get_running_loop().time()))
        if self.tasks:
            pending = asyncio.gather(*self.tasks)
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                await pending
                raise


class PlainTextStatusFeeler(StatusFeeler):
    """Infrequent activity notices for channels without editable indicators.

    Sent notices remain historical messages. Clearing stops updates and sends no
    completion claim; only the run's actual output reports its outcome.
    """

    update_interval: ClassVar[float] = 10.0
    refresh_interval: ClassVar[float] = float("inf")

    def __init__(self, segments: SegmentsFeeler) -> None:
        super().__init__()
        self.segments = segments

    async def present(self, address: ChannelAddress, text: str) -> None:
        if text:
            await self.segments.present(
                address, [TextSegment(data={"text": f"Activity update: {text}"})]
            )
