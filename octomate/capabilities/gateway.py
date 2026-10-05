"""Gateway capability: an agent's routing spellbook.

The spells decide where a turn goes, who handles it, and what it is about. Each is
opaque on its own, so the instruction opens with plain words for what they actually do:

- `inspect`: reveal agent routes, suggested addresses, or projects.
- `summon`: hand the conversation to another agent (a handoff — they take over from a
  brief). The graph reads the recorded decision after the run.
- `teleport`: continue the same agent in a new thread, carrying the
  history forward. Deferred, so the graph can fork the history and resume. With a
  `project`, the place is that project's workspace — the door out of a throwaway
  tree into one whose work is kept — and `new_thread=false` binds the current thread.
- `dismiss`: give a project thread's workspace back once the work in it is done.
  Recorded, and performed by the graph when the turn ends and its work is saved.
- `commission`: draw another agent into working a self-contained task in the background
  and return its report — an ordinary awaited tool call, never a deferral; the caller
  keeps the conversation and the user sees none of it.
- `whisper`: a quiet follow-up to an accomplice by name; it keeps its own context.
- `send`: deliver content mid-run without ending the turn, here or in the
  asking user's direct messages. It lives here rather than in its own capability
  because naming somewhere other than "here" is a routing decision, and this is
  where the channel registry and the run's own address already are.

The policy itself — what each spell may name, and the decision it records — lives on
`octomate.managers.gateway.OctomateSession`; this capability is Inkling's translation
of it into pydantic-ai: refusals become `ModelRetry`, a teleport becomes a deferral,
and a send rides the run event stream. The accomplice spells stay wholly here — they
spawn pydantic-ai runs, which no other runtime's gateway offers.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, cast

from octomate_protocol.gateway import (
    GATEWAY_TOOLSET_ID,
    GatewayTool,
)
from pydantic import Field
from pydantic_ai import AgentStreamEvent, CallDeferred, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import (
    FunctionToolResultEvent,
    ToolReturn,
    ToolReturnPart,
)
from pydantic_ai.settings import ThinkingEffort
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from octomate.capabilities.harness.events import MessageSentEvent
from octomate.managers.gateway import GatewayRefusal, OctomateSession
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.segments import MessageSegment
from octomate.schemas.triage import (
    DIRECT_TARGET,
    HERE_TARGET,
    AgentRoute,
    GatewayDecision,
    InspectFacet,
    ProjectSummary,
    SchemeTarget,
    SendTarget,
)

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from octomate.managers.conversation import ConversationManager
    from octomate.tentacles.agent import AgentTentacle

# A commission holds the parent's live tool call open while the accomplice runs, so the
# wait must not be unbounded (`approval_timeout` is the precedent). Seconds.
COMMISSION_TIMEOUT = 900.0

# The instruction prose, templated only where a spell is named: each runtime's
# adapter renders the same contract under its own tool naming (`inspect` for Inkling,
# `gateway_inspect` on the served server). Argument names and address fields are
# shared by both surfaces and stay literal.
GATEWAY_INSTRUCTION_TEMPLATE = """\
## Gateway — decide where this conversation goes and who handles it

These tools route the conversation. Default to handling it yourself: if you can answer
well or do the work, do it and call none of them. Routing is the exception — reach for a
tool only when one of the signals below clearly fires.

### Addresses and discovery
For `{teleport}`, `destination` is a `ChannelAddress` object, or
`destination=null` to use the current conversation's address. An address carries
`channel_tentacle_id`, `chat_type`, `chat_id`, `user_id`, and optional
`channel_thread_id` and `shared` visibility. Use known identifiers and preserve
visibility; `user_id` must match the requesting user's linked identity on that
channel. When no current conversation is attached, supply an explicit address.

`{inspect}` with `reveal="destinations"` suggests addresses without creating anything.
To look further, add `channel` to browse one connected channel a level at a time:
an address with `metadata.inside` is a place to open by passing that value as
`inside`, and one without it can host a new thread unless `metadata.barred` says
why not. A channel that cannot be browsed says so.
You can also supply a known address directly; discovery is optional, and each
operation validates the address independently. Suggestions are not an exhaustive
list or a guarantee that every spell can use each address.
`new_thread=true` is the default: create a thread at the address. For creation,
use `channel_thread_id=null`; an existing thread cannot contain another thread.

### `{summon}` — hand off to another agent
Summon transfers the conversation to a specialist who takes over this turn *and its
follow-ups*: a real, sticky handoff, so the bar is high. Summon only when:
- The request needs a capability you lack — e.g. running or editing code in a real
  repository or environment, or a domain another agent is described for.
- It is substantial specialist work another agent would do markedly better, not
  something you can handle from what you already know.

Do NOT summon when:
- You can already answer or do it — length or a technical-sounding topic is not a reason.
- You are only mildly unsure — ask the user a clarifying question instead.
- No route clearly fits — handle it yourself or ask; never summon on a guess.

When one fires, call `{inspect}` with `reveal="routes"` first to see the agents and what
each is for. Every route carries a claim: its ability (what that agent+model is for)
and the effort levels it accepts — pick the route whose ability covers the work. Set
`effort` only when the user explicitly asked for a level; otherwise leave it unset so
the agent's own default applies. Then `{summon}` — copying its `agent_id` and `model`
exactly from that route, and writing a self-contained brief since the other agent may
not see this chat. Summon hands over this conversation where it is; to continue
somewhere else, `{teleport}` there with `resume=true` and summon from the thread you
land in. It is refused on a group's main conversation and in native sessions. You
yourself are not a valid summon target.

### `{teleport}` — relocate yourself
Move this conversation into a new thread that *you* keep handling, carrying
everything said so far. Use it for multi-step or long-running work that deserves its
own thread but that you are the right one to do — no other agent involved.
Omit `destination` or use null for the current address, or supply another address.
Teleport requires attached conversation history that the agent can copy, and the
destination must support continuing that agent. Shared history can only move into
a sub-thread of the current chat; moving to another address requires a private
source. If a requested new thread cannot be created, the move fails without
silently continuing at its parent.

To work on a project, add `project` (from `{inspect}` with `reveal="projects"`), and
`ref` — a branch, tag or commit — only when the default branch is the wrong place to
start: the thread you land in is bound to it and you resume in its workspace, where
work is kept. A thread about no project runs in a throwaway tree, so do this before
you start work, not after. From inside a thread, use `destination=null`,
`new_thread=false` and `project` to bind this one without opening another.
Staying put requires a project; a thread binds once, and a different project is
a different thread. Native sessions must move into a new thread.

### `{scheme}` — take it to the user privately
Continue one-to-one with the person who asked, in their direct messages: for work that
is theirs alone, or that does not belong in front of the group. Whoever already handles
their direct messages picks it up, so write `brief` self-contained — it may not be you,
and they cannot see this chat. `hint` opens the conversation over there and is the
first thing they read; nothing is posted here, so close out your own reply by saying
the work is moving, not what it is. Its destination is a target object with
`kind="dm"`, optionally naming a connected `channel` ID for the user's linked
account there. Omitting the destination uses this channel's DM; that local move
requires a shared source. Another connected channel's DM can also be selected
from a private source. The tool refuses unsupported or unlinked destinations.

### `{dismiss}` — give the workspace back when the work is done
A thread about a project keeps its workspace between turns, which is what makes its
next turn a resume. When the work is finished for good — merged, delivered, or
dropped — say so with `{dismiss}`: the tree is released when this turn ends, after
this turn's work is saved to the project's mirror, and a later message here forks it
afresh from there, so nothing is lost but the disk. Not for a pause — a thread
waiting on someone keeps its tree, and idle ones are swept on their own. Wrap up in
the same turn; your working directory goes with it.
"""

# Inkling's handoff guidance, appended for Inkling alone: the other runtimes bring
# their own — a skill, a plugin, a harness's instructions — and the contract every
# runtime shares is the docstring, which tells them to follow what they carry.
HANDOFF_INSTRUCTION = """

### Writing a brief
A `summon` or `scheme` brief is the receiver's whole opening prompt, and it is
budgeted. Say, in this order and only where it changes what they do next: the goal
in the user's own words; the constraints they set; the decisions made and why; what
is done; what was tried and failed, so it is not tried again; what is left, first
item first; and what a finished result looks like. Leave the rest to the ledger:
the receiver reads every thread this person has spoken in, so cite a message by
its `#msg:<id>` handle and name what to search for instead of repeating it. Do
not restate the project — the workspace's own instructions carry that.
"""

# The framing every accomplice run carries, passed by the gateway at the
# `subagent_run` call site: no gate, no user, no approvals — the reply is the
# report.
ACCOMPLICE_INSTRUCTION = """\
You are an accomplice: another agent commissioned you, and your reply is
your report back to it. There is no user here to ask, and anything that needs a
human approval is declined immediately — work from the brief you were given within
what you can do unaided; if something is under-specified or unapprovable, state the
assumption or the blocker in your report and proceed.
"""

SEND_INSTRUCTION_TEMPLATE = """\

### `{send}` — deliver something now, without ending your turn
For a progress update, an intermediate result, or an image/file you produced along
the way. Anything sent this way is already delivered: your final reply continues from
there — summarize or extend it, never restate it. If everything worth saying went out
already, close with a short wrap-up rather than re-sending it.

`destination` defaults to a target object with `kind="here"`. A target with
`kind="dm"` delivers to the person who asked, privately —
for something that is *for them*, like a summary sent over; say in your reply that you
sent it. `dm` hands nothing over: you keep this conversation and nobody picks the work
up there, so use `{scheme}` when the work itself should continue privately. Asking for
`dm` while already in that person's direct messages is fine — it lands here.
A DM target can name a connected `channel` ID to reach their linked account there.
If no current conversation is attached, that channel must be explicit. Use the
selected address's `channel_tentacle_id` as `channel`; the recipient is resolved
from the user's linked profile.

To thread onto a specific message in a busy chat, lead with a reply segment whose id
is that message's `#msg:<id>` handle — it must be the first segment. To ping someone,
use an `at` segment with their user id.
"""


def gateway_instructions(tool_name: Callable[[GatewayTool], str]) -> str:
    """The gateway's routing instruction, each spell rendered by the caller's own tool
    naming — the identity for Inkling, `gateway_…` on the served server — so every
    agent reads one contract under the names its runtime lists the tools by."""
    names = {
        "inspect": tool_name(GatewayTool.INSPECT),
        "summon": tool_name(GatewayTool.SUMMON),
        "teleport": tool_name(GatewayTool.TELEPORT),
        "scheme": tool_name(GatewayTool.SCHEME),
        "send": tool_name(GatewayTool.SEND),
        "dismiss": tool_name(GatewayTool.DISMISS),
    }
    return GATEWAY_INSTRUCTION_TEMPLATE.format(
        **names
    ) + SEND_INSTRUCTION_TEMPLATE.format(**names)


COMMISSION_INSTRUCTION = """\

### `commission` — put another agent to work in the background (you keep the conversation)
Where `summon` hands the conversation away, `commission` does not: another agent works a
self-contained task and the tool returns its report — the user sees only your reply.
Call `inspect` with `reveal="routes"` and no `channel` to choose a route from this
conversation's channel; the same claim and effort rules as `summon` apply.
Give the accomplice a short mnemonic `name`. The brief must stand alone: the
accomplice cannot see this chat and has no user to ask, so include the goal, the
relevant context, and what a finished result looks like. Several commissions in one
reply run concurrently.

### `whisper` — a quiet word to an accomplice
Continue an accomplice's work by `name`: it remembers everything it did. Use it to
refine or extend that work instead of commissioning a new accomplice.
"""


@dataclass
class GatewayCapability(AbstractCapability[None]):
    """The gateway spells as one turn's toolset: inspect, summon, teleport, scheme,
    send and dismiss, plus the accomplice spells when there is a thread to run
    them in."""

    # The turn's policy and decision slot, shared with the graph that acts on it.
    session: OctomateSession
    # What running an accomplice takes, beyond the session's own deps. None on a
    # gateway built only to route, which then does not offer the accomplice spells.
    conversations: ConversationManager | None = None
    commission_timeout: float = COMMISSION_TIMEOUT
    commissioning: bool = field(default=False, init=False)
    toolset: FunctionToolset[None] | None = field(default=None, init=False, repr=False)

    @property
    def decision(self) -> GatewayDecision | None:
        return self.session.decision

    def __post_init__(self) -> None:
        # Nothing built from runtime state may reach a tool definition: they are a
        # provider prompt-cache breakpoint (`anthropic_cache_tool_definitions`) at the
        # front of the prefix, so a schema that varies forks it into variants that never
        # warm each other. Hence plain `str` routes, validated by `claimed_route`
        # against the list `inspect` returns — a tool *result*, after the breakpoint.
        toolset: FunctionToolset[None] = FunctionToolset(id=GATEWAY_TOOLSET_ID)
        toolset.tool(name=GatewayTool.INSPECT)(self.inspect)
        toolset.tool(name=GatewayTool.SUMMON, retries=2)(self.summon)
        # `retries` to match its siblings: teleport refuses a surface with no
        # sub-thread to open, so it needs the same room to be told and correct.
        toolset.tool(name=GatewayTool.TELEPORT, retries=2, requires_approval=True)(
            self.teleport
        )
        toolset.tool(name=GatewayTool.SCHEME, retries=2)(self.scheme)
        toolset.tool(name=GatewayTool.SEND, retries=2)(self.send)
        toolset.tool(name=GatewayTool.DISMISS, retries=2)(self.dismiss)
        if (
            self.session.agents is not None
            and self.conversations is not None
            and self.session.thread_id is not None
            and self.session.conversation_address is not None
        ):
            self.commissioning = True
            toolset.tool(name=GatewayTool.COMMISSION, retries=2)(self.commission)
            toolset.tool(name=GatewayTool.WHISPER, retries=2)(self.whisper)
        self.toolset = toolset

    def commission_deps(
        self,
    ) -> tuple[
        dict[str, AgentTentacle], ConversationManager, uuid.UUID, ChannelAddress
    ]:
        """The live handles the accomplice spells run with. Registration only
        offers the spells when all four are set, so a miss here is a
        construction bug, not a model mistake."""
        if (
            self.session.agents is None
            or self.conversations is None
            or self.session.thread_id is None
            or self.session.conversation_address is None
        ):
            raise RuntimeError(
                "the accomplice spells need agents, conversations, a thread and an address"
            )
        return (
            self.session.agents,
            self.conversations,
            self.session.thread_id,
            self.session.conversation_address,
        )

    async def run_accomplice(
        self,
        ctx: RunContext[None],
        *,
        child: Conversation,
        run_name: str,
        prompt: str,
        model: Model | str | None,
        effort: ThinkingEffort | None,
    ) -> str:
        agents, conversations, thread_id, conversation_address = self.commission_deps()
        if ctx.run_id is None:
            raise RuntimeError("a commission needs the parent run id for the run tree")
        agent = agents[child.agent_tentacle_id]
        try:
            # The subagent contract (non-interactive, no capabilities,
            # addressed at the pre-ensured child conversation) lives on
            # `subagent_run`; the accomplice framing is this spawner's and is
            # passed here. An accomplice that defers anyway ends its run with
            # DeferredToolRequests, surfaced loudly below instead of parking a
            # batch nothing resumes.
            result = await asyncio.wait_for(
                agent.subagent_run(
                    prompt,
                    conversation_address=conversation_address,
                    thread_id=thread_id,
                    conversation_id=child.id,
                    run_name=run_name,
                    model=model,
                    effort=effort,
                    instructions=ACCOMPLICE_INSTRUCTION,
                ),
                timeout=self.commission_timeout,
            )
        except TimeoutError:
            raise ModelRetry(
                f"The accomplice {child.subagent_id!r} exceeded "
                f"{self.commission_timeout:.0f}s and was stopped. What it "
                f"recorded is kept — `{GatewayTool.WHISPER}` to continue "
                "it, or break the work into smaller briefs."
            ) from None
        # The runner recorded its turn without knowing its place;
        # the spawner stamps the run tree after the fact.
        await conversations.link_parent_run(
            result.run_id,
            parent_run_id=ctx.run_id,
            parent_tool_call_id=ctx.tool_call_id,
        )
        output = result.output
        if isinstance(output, DeferredToolRequests):
            raise ModelRetry(
                f"The accomplice {child.subagent_id!r} tried to ask the "
                "user or defer — an accomplice has no user. Write a "
                "self-contained brief that needs no clarification, then "
                "commission another."
            )
        if isinstance(output, str):
            return output
        if isinstance(output, Iterable):
            return "\n\n".join(str(part) for part in output)
        return str(output)

    async def inspect(
        self,
        ctx: RunContext[None],
        reveal: InspectFacet,
        channel: str | None = None,
        inside: str | None = None,
    ) -> list[AgentRoute] | list[ChannelAddress] | list[ProjectSummary]:
        """Reveal one facet of what this conversation can reach.

        Args:
            reveal: `routes` — agent/model choices on the current or selected channel.
                Summon and commission use the current channel's routes.
                `destinations` — optional address suggestions, validated independently
                when used; known addresses need not appear in this list. With
                `channel`, one level of that channel's addresses instead.
                `projects` — the projects this deployment can work on, for `teleport`.
            channel: A connected channel ID from an address. For `routes`, omit it
                to inspect the current conversation's routes. For `destinations`,
                name it to browse that channel as the requesting user: an address
                whose `metadata.inside` is set is a place to open, one without it
                can host a new thread unless `metadata.barred` says why not.
            inside: For `destinations` with `channel`, the `metadata.inside` of a
                listed address, to list what that place holds. Omit it for the
                channel's top level.
        """
        try:
            return await self.session.inspect(reveal, channel, inside)
        except GatewayRefusal as refusal:
            raise ModelRetry(str(refusal)) from refusal

    async def summon(
        self,
        ctx: RunContext[None],
        agent_id: str,
        model: str,
        hint: str,
        reason: str,
        summon: Annotated[str, Field(max_length=8_000)],
        effort: ThinkingEffort | None = None,
    ) -> str:
        """Hand this conversation to another Octomate agent, who takes it over where
        it is.

        Args:
            agent_id: The target agent, copied exactly from an `inspect` route
                (`reveal="routes"`).
            model: That route's model, copied exactly.
            hint: A short, user-facing note announcing the handoff, recorded with it.
            reason: One line on why this agent fits — recorded with the handoff, not
                shown to the user as the reply.
            summon: The self-contained brief the other agent starts from. It becomes
                their opening prompt and they cannot see this conversation, so give
                the goal, the relevant context and decisions, what's been tried, and
                what a finished result looks like. If anything you carry says how to
                hand off — a handoff skill, a plugin, your own harness's instructions
                — follow it; this is the floor. Reference rather than repeat: the
                receiver reads every thread this person has spoken in, so cite a
                message by its `#msg:<id>` handle and say what to search for instead
                of pasting it. A brief over the size budget is refused, never trimmed.
            effort: How hard the agent should think, from the effort levels the
                route's claim offers. Set it only when the user explicitly asked
                for a level; omitted, the agent's own default applies.
        """
        try:
            return await self.session.summon(
                agent_id=agent_id,
                model=model,
                hint=hint,
                reason=reason,
                summon=summon,
                effort=effort,
            )
        except GatewayRefusal as refusal:
            raise ModelRetry(str(refusal)) from refusal

    async def teleport(
        self,
        ctx: RunContext[None],
        hint: str,
        destination: ChannelAddress | None = None,
        project: str | None = None,
        ref: str | None = None,
        new_thread: bool = True,
        resume: bool = False,
    ) -> str:
        """Continue this conversation yourself somewhere else; everything said so
        far comes with you. This turn ends on it, and your context is intact
        there.

        Args:
            hint: The short, user-facing thread-starter message.
            destination: The address at which to create a thread; omit for the
                current chat. The owning channel validates it independently of
                discovery and requires the requesting user's linked identity.
                Shared history stays under its current parent. Teleport requires
                attached history that the agent can copy.
            new_thread: Create a thread before continuing. Set false only to bind
                the current thread to a project without opening another one.
                For creation, the address must have no `channel_thread_id`.
            project: A project's name, copied exactly from `inspect`
                (`reveal="projects"`), for a conversation about no project: the
                thread you land in is bound to it, and you resume in its workspace,
                where work is kept. A conversation about a project carries it
                wherever it goes, so omit this there.
            ref: The branch, tag or commit that workspace starts from; omit it for
                the project's default branch.
            resume: Carry on working there at once, as after any tool call. Leave
                it false when the move is all that was asked: you continue when
                the user next writes there.
        """
        try:
            decision = await self.session.teleport(
                hint=hint,
                destination=destination,
                new_thread=new_thread,
                project=project,
                ref=ref,
                resume=resume,
            )
        except GatewayRefusal as refusal:
            raise ModelRetry(str(refusal)) from refusal
        raise CallDeferred(metadata=decision.metadata())

    async def scheme(
        self,
        ctx: RunContext[None],
        hint: str,
        brief: Annotated[str, Field(max_length=8_000)],
        destination: SchemeTarget = DIRECT_TARGET,
    ) -> str:
        """Continue this with the user one-to-one, in their direct messages.

        Whoever already handles that user's direct messages picks the work up — you do
        not choose them, and their direct messages keep the owner they had. Use it for
        work that is that person's alone, or that does not belong in front of the group.

        Args:
            hint: The short, user-facing line that opens the conversation over there,
                where they will read it before anything else. Say that the work is
                being picked up — not the brief, which is written for whoever answers.
            brief: The self-contained brief whoever answers there starts from. They
                cannot see this conversation, so give the goal, the relevant context and
                decisions, what's been tried, and what a finished result looks like. If
                anything you carry says how to hand off — a handoff skill, a plugin,
                your own harness's instructions — follow it; this is the floor.
                Reference rather than repeat: the receiver reads every thread this
                person has spoken in, so cite a message by its `#msg:<id>` handle and
                say what to search for instead of pasting it. A brief over the size
                budget is refused, never trimmed.
            destination: A target object with `kind="dm"`. Omit `channel` for this
                channel's DM when moving from a shared source, or name another
                connected channel ID, including from a private source. The user's
                linked profile determines the recipient.
        """
        try:
            return await self.session.scheme(
                hint=hint, brief=brief, destination=destination
            )
        except GatewayRefusal as refusal:
            raise ModelRetry(str(refusal)) from refusal

    async def send(
        self,
        ctx: RunContext[None],
        segments: list[MessageSegment],
        destination: SendTarget = HERE_TARGET,
    ) -> ToolReturn[str]:
        """Deliver these segments immediately, without ending your turn — a progress
        update, an intermediate result, an image or a file. What you send here is
        already delivered: do NOT repeat it in your final reply.

        Args:
            segments: What to deliver.
            destination: A target object with `kind="here"` (the default) or
                `kind="dm"`. A DM target can name a connected `channel` ID; omit
                it for this channel. With no current conversation, name a channel
                explicitly. When choosing from an inspected address, use its
                `channel_tentacle_id` as `channel`; the linked profile supplies the
                recipient. Say in your reply when you sent it elsewhere.
        """
        try:
            address = await self.session.resolve_send(destination)
        except GatewayRefusal as refusal:
            raise ModelRetry(str(refusal)) from refusal
        return ToolReturn(
            return_value="sent",
            metadata=[MessageSentEvent(segments=segments, destination=address)],
        )

    async def dismiss(self, ctx: RunContext[None]) -> str:
        """Give this thread's workspace back, now that the work in it is done —
        merged, delivered, or dropped for good, not paused. It is released when
        this turn ends, after this turn's work is saved to the project's mirror,
        so nothing is lost: a later message on this thread forks it afresh from
        there. Only the disk goes, and it goes now rather than after the idle
        window the sweep would otherwise wait out.
        """
        try:
            return await self.session.dismiss()
        except GatewayRefusal as refusal:
            raise ModelRetry(str(refusal)) from refusal

    async def commission(
        self,
        ctx: RunContext[None],
        name: str,
        agent_id: str,
        model: str,
        brief: str,
        effort: ThinkingEffort | None = None,
    ) -> str:
        """Draw another Octomate agent into working a task and get its report
        back — the user sees none of it.

        Args:
            name: Your name for this accomplice — short and mnemonic, e.g.
                `repo-audit`. `whisper` to it later to follow up.
            agent_id: The agent to draw in, copied exactly from an
                `inspect` route for this conversation's channel (omit `channel`).
            model: That route's model, copied exactly.
            brief: The self-contained work order. The accomplice cannot see
                this conversation and has no user to ask, so give the
                goal, the relevant context and decisions, and what a
                finished result looks like.
            effort: How hard the accomplice should think, from the levels the
                route's claim offers. Set it only when the user
                explicitly asked for a level.
        """
        agents, conversations, thread_id, _ = self.commission_deps()
        if not name.strip():
            raise ModelRetry("Give the accomplice a short, mnemonic name.")
        if agent_id == self.session.current_agent_id:
            raise ModelRetry(
                f"Cannot commission yourself {self.session.current_agent_id!r}. "
                f'Call `{GatewayTool.INSPECT}` with `reveal="routes"` to choose a valid route.'
            )
        try:
            route = self.session.claimed_route(
                agent_id, model, effort, spell="commission"
            )
        except GatewayRefusal as refusal:
            raise ModelRetry(str(refusal)) from refusal
        run_model = agents[agent_id].models.get(route.model)
        if run_model is None:
            raise ModelRetry(
                f"Agent {agent_id!r} does not serve model {model!r}. "
                f'Call `{GatewayTool.INSPECT}` with `reveal="routes"` and copy a route exactly.'
            )
        # The calling run's own conversation is the parent — the react
        # graph put its id on the RunContext. No id means the gate is
        # mounted outside a live run: raise, never conjure a parent.
        if ctx.conversation_id is None:
            raise RuntimeError("a commission needs the calling run's conversation id")
        parent_id = uuid.UUID(ctx.conversation_id)
        child = await conversations.ensure(
            thread_id,
            agent_tentacle_id=agent_id,
            subagent_id=name,
            parent_conversation_id=parent_id,
        )
        if child.parent_conversation_id != parent_id or child.runs:
            raise ModelRetry(
                f"{name!r} is already at work — `{GatewayTool.WHISPER}` "
                "to it, or pick a new name."
            )
        return await self.run_accomplice(
            ctx,
            child=child,
            run_name="commission",
            prompt=brief,
            model=run_model,
            effort=effort,
        )

    async def whisper(self, ctx: RunContext[None], name: str, message: str) -> str:
        """A quiet word to an accomplice you commissioned. It keeps
        everything it did — your whisper continues its work, not a fresh
        start.

        Args:
            name: The name you gave the accomplice when you commissioned it.
            message: The follow-up work order — self-contained, like a
                brief; the accomplice still cannot see this conversation.
        """
        _, conversations, thread_id, _ = self.commission_deps()
        if ctx.conversation_id is None:
            raise RuntimeError("a whisper needs the calling run's conversation id")
        parent_id = uuid.UUID(ctx.conversation_id)
        hands = await conversations.subagents(parent_id)
        stored = next((hand for hand in hands if hand.subagent_id == name), None)
        if stored is None:
            live = ", ".join(sorted(hand.subagent_id for hand in hands))
            raise ModelRetry(
                f"No accomplice named {name!r}. "
                + (
                    f"Accomplices at work: {live}."
                    if live
                    else "No accomplices are at work."
                )
                + f" `{GatewayTool.COMMISSION}` one to start."
            )
        child = await conversations.ensure(
            thread_id,
            agent_tentacle_id=stored.agent_tentacle_id,
            subagent_id=name,
            parent_conversation_id=parent_id,
        )
        return await self.run_accomplice(
            ctx,
            child=child,
            run_name="whisper",
            prompt=message,
            model=None,
            effort=None,
        )

    def get_instructions(self) -> str:
        instructions = gateway_instructions(lambda name: name) + HANDOFF_INSTRUCTION
        if self.commissioning:
            return instructions + COMMISSION_INSTRUCTION
        return instructions

    async def wrap_run_event_stream(
        self,
        ctx: RunContext[None],
        *,
        stream: AsyncIterable[AgentStreamEvent],
    ) -> AsyncIterable[AgentStreamEvent]:
        """Put each `send`'s stashed `MessageSentEvent` on the run stream,
        where the consumer rendering this run delivers it."""
        async for event in stream:
            yield event
            if (
                isinstance(event, FunctionToolResultEvent)
                and isinstance(event.part, ToolReturnPart)
                and event.part.tool_name == GatewayTool.SEND
                and isinstance(event.part.metadata, list)
            ):
                for sent_event in event.part.metadata:
                    # One dynamic-boundary cast: pydantic-ai types the stream as
                    # AgentStreamEvent, consumers match the concrete octomate event.
                    yield cast(AgentStreamEvent, sent_event)

    def get_toolset(self) -> AbstractToolset[None] | None:
        return self.toolset
