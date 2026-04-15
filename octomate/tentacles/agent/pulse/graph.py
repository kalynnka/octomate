from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, cast

from pydantic_ai.tools import DeferredToolRequests
from pydantic_graph import BaseNode, End, Graph, GraphRunContext
from uuid_utils import uuid7

from octomate.schemas.actions import AgentMessage
from octomate.schemas.segments import TextSegment
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.agent.pulse.run import resolve_deferred, streaming
from octomate.tentacles.agent.pulse.state import PulseDeps, PulseState, SubAgent
from octomate.transmuters.interactions import Todo

logger = logging.getLogger(__name__)

BECKON_TIMEOUT = 900.0  # 15 minutes


@dataclass
class Triage(BaseNode[PulseState, PulseDeps, list[AgentMessage]]):
    """Classify the request: answer directly, produce a plan, or summon an agent."""

    async def run(
        self, ctx: GraphRunContext[PulseState, PulseDeps]
    ) -> ExecuteStep | End[list[AgentMessage]]:
        key = ctx.deps.agent_deps.session_key if ctx.deps.agent_deps else None
        logger.info("[%s] Triage started", key)

        if ctx.deps.stream:
            await ctx.deps.stream.set_status("Thinking…")

        result = await streaming(
            ctx.deps.pulse_agent,
            ctx.deps.stream,
            user_prompt=ctx.state.prompt,
            deps=ctx.deps.agent_deps,
            toolsets=ctx.deps.toolsets,
            instructions=ctx.deps.instructions,
            message_history=ctx.deps.message_history,
        )
        if isinstance(result.output, DeferredToolRequests):
            logger.debug("[%s] Triage returned deferred tools, resolving", key)
            result = await resolve_deferred(
                ctx.deps.pulse_agent,
                result,
                ctx.deps.tentacle,
                ctx.deps.agent_deps,
                ctx.deps.toolsets,
                ctx.deps.stream,
            )

        ctx.state.main_history = result.all_messages()

        output = result.output
        if isinstance(output, list) and output and isinstance(output[0], Todo):
            ctx.state.todos = [t for t in output if isinstance(t, Todo)]
            logger.info("[%s] Triage produced %d plan steps", key, len(ctx.state.todos))

            if ctx.deps.stream:
                await ctx.deps.stream.set_status("Planning…")

            if ctx.deps.tentacle and key:
                persisted = []
                for t in ctx.state.todos:
                    db_todo = await ctx.deps.tentacle.feelers.todos.create_todo(
                        key, title=t.title
                    )
                    persisted.append(
                        t.model_copy(update={"todo_id": db_todo.todo_id})
                        if db_todo.todo_id
                        else t
                    )
                ctx.state.todos = persisted
                ctx.state.card_ref = (
                    await ctx.deps.tentacle.feelers.todos.upsert_todo_list(
                        key, ctx.state.todos
                    )
                )
                if ctx.state.card_ref:
                    await ctx.deps.tentacle.feelers.todos.pin_todo(
                        key, ctx.state.card_ref
                    )

            return ExecuteStep()
        logger.info("[%s] Triage answered directly", key)
        return End(output)  # type: ignore[arg-type]


@dataclass
class ExecuteStep(BaseNode[PulseState, PulseDeps]):
    """Execute all ready Todo steps, dispatching by tier."""

    async def run(
        self, ctx: GraphRunContext[PulseState, PulseDeps]
    ) -> ExecuteStep | Synthesize:
        key = ctx.deps.agent_deps.session_key if ctx.deps.agent_deps else None
        completed_ids = {t.todo_id for t in ctx.state.todos if t.status == "completed"}
        pending = [t for t in ctx.state.todos if t.status == "pending"]
        ready = [t for t in pending if all(d in completed_ids for d in t.depends_on)]

        if not ready:
            if pending:
                raise RuntimeError(
                    f"plan stalled: {len(pending)} pending todos with unmet deps"
                )
            logger.info("[%s] All steps done, moving to synthesize", key)
            return Synthesize()

        inline: list[Todo] = []
        subagent_jobs: list[tuple[Todo, SubAgent]] = []
        tentacle_jobs: list[tuple[Todo, AgentTentacle]] = []
        for todo in ready:
            if todo.assignee is None:
                inline.append(todo)
            elif todo.assignee in ctx.deps.subagents:
                subagent_jobs.append((todo, ctx.deps.subagents[todo.assignee]))
            elif todo.assignee in ctx.deps.tentacles:
                tentacle_jobs.append((todo, ctx.deps.tentacles[todo.assignee]))
            else:
                logger.warning(
                    "[%s] Unknown assignee %r for step %r, falling back to inline",
                    key,
                    todo.assignee,
                    todo.todo_id,
                )
                inline.append(todo)

        if ctx.deps.stream:
            if len(ready) > 1:
                titles = ", ".join(t.title for t in ready[:3])
                suffix = f" (+{len(ready) - 3} more)" if len(ready) > 3 else ""
                await ctx.deps.stream.set_status(
                    f"Working on {len(ready)} steps in parallel: {titles}{suffix}"
                )
            else:
                await ctx.deps.stream.set_status(f"Working on: {ready[0].title}")

        tasks = []
        if inline:
            tasks.append(self._run_inline_chain(ctx, key, inline))
        for todo, sub in subagent_jobs:
            tasks.append(self._run_subagent(ctx, todo, sub))
        for todo, tent in tentacle_jobs:
            tasks.append(self._run_beckon(ctx, key, todo, tent))
        await asyncio.gather(*tasks)

        if ctx.deps.tentacle and key:
            for todo in ready:
                if todo.todo_id:
                    await ctx.deps.tentacle.feelers.todos.update_todo(
                        todo.todo_id, "completed"
                    )
            ctx.state.card_ref = await ctx.deps.tentacle.feelers.todos.upsert_todo_list(
                key, ctx.state.todos, existing_ts=ctx.state.card_ref
            )

        return ExecuteStep()

    async def _run_inline_chain(
        self,
        ctx: GraphRunContext[PulseState, PulseDeps],
        key: Any,
        todos: list[Todo],
    ) -> None:
        for todo in todos:
            logger.info("[%s] Inline step: %s", key, todo.title)
            user_prompt = (
                f"Execute step {todo.title!r}: {todo.description}\n\n"
                "Return ONLY the step result as plain text. "
                "Do not re-plan or summarize other steps."
            )
            result = await streaming(
                ctx.deps.pulse_agent,
                ctx.deps.stream,
                user_prompt=user_prompt,
                output_type=str,
                deps=ctx.deps.agent_deps,
                toolsets=ctx.deps.toolsets,
                instructions=ctx.deps.instructions,
                message_history=ctx.state.main_history,
            )
            if isinstance(result.output, DeferredToolRequests):
                result = await resolve_deferred(
                    ctx.deps.pulse_agent,
                    result,
                    ctx.deps.tentacle,
                    ctx.deps.agent_deps,
                    ctx.deps.toolsets,
                    ctx.deps.stream,
                )
            ctx.state.step_outputs[todo.todo_id] = str(result.output)
            ctx.state.main_history = result.all_messages()
            ctx.state.inline_todo_ids.add(todo.todo_id)
            ctx.state.todos = [
                t.model_copy(update={"status": "completed"})
                if t.todo_id == todo.todo_id
                else t
                for t in ctx.state.todos
            ]

    async def _run_subagent(
        self,
        ctx: GraphRunContext[PulseState, PulseDeps],
        todo: Todo,
        subagent: SubAgent,
    ) -> None:
        key = ctx.deps.agent_deps.session_key if ctx.deps.agent_deps else None
        logger.info("[%s] SubAgent %r: %s", key, subagent.id, todo.title)
        output = await subagent.execute(
            key,
            todo,
            ctx.state,
            ctx.deps.agent_deps,
            stream=ctx.deps.stream,
        )
        ctx.state.step_outputs[todo.todo_id] = output
        ctx.state.todos = [
            t.model_copy(update={"status": "completed"})
            if t.todo_id == todo.todo_id
            else t
            for t in ctx.state.todos
        ]

    async def _run_beckon(
        self,
        ctx: GraphRunContext[PulseState, PulseDeps],
        key: Any,
        todo: Todo,
        agent_tentacle: AgentTentacle,
    ) -> None:
        from octomate.nerve import AgentResult, SummonAgent
        from octomate.schemas.events import MessageEvent

        rid = str(uuid7())
        future = ctx.deps.tentacle.octopus.pending.agent_results.create(rid)

        summary = todo.description or todo.title
        content = MessageEvent(
            tentacle_id=key.tentacle_id,
            user_id=key.user_id,
            chat_id=key.chat_id,
            chat_type="group" if key.group_id else "private",
            segments=[TextSegment(data={"text": summary})],
        )
        await ctx.deps.tentacle.octopus.agent_nerve.send(
            SummonAgent(
                key=key,
                agent_tag=agent_tentacle.id,
                contents=[content],
                summary=summary,
                request_id=rid,
                silent=True,
            )
        )
        logger.info(
            "[%s] Beckoned %r for step %r (request_id=%s)",
            key,
            agent_tentacle.id,
            todo.title,
            rid,
        )

        try:
            result: AgentResult = await asyncio.wait_for(future, timeout=BECKON_TIMEOUT)
            output = result.output or "(no output)"
        except (TimeoutError, asyncio.CancelledError) as exc:
            ctx.deps.tentacle.octopus.pending.agent_results.cancel(rid)
            if isinstance(exc, TimeoutError):
                logger.warning(
                    "[%s] Beckoned agent %r timed out for step %r",
                    key,
                    agent_tentacle.id,
                    todo.title,
                )
                output = "(beckoned agent timed out)"
            else:
                raise

        ctx.state.step_outputs[todo.todo_id] = output
        ctx.state.todos = [
            t.model_copy(update={"status": "completed"})
            if t.todo_id == todo.todo_id
            else t
            for t in ctx.state.todos
        ]


@dataclass
class Synthesize(BaseNode[PulseState, PulseDeps, list[AgentMessage]]):
    """Produce the final user-facing answer from all step outputs."""

    async def run(
        self, ctx: GraphRunContext[PulseState, PulseDeps]
    ) -> End[list[AgentMessage]]:
        key = ctx.deps.agent_deps.session_key if ctx.deps.agent_deps else None
        logger.info(
            "[%s] Synthesize started (%d step outputs)",
            key,
            len(ctx.state.step_outputs),
        )

        if ctx.deps.stream:
            await ctx.deps.stream.set_status("Wrapping up…")

        non_inline = {
            tid: out
            for tid, out in ctx.state.step_outputs.items()
            if tid not in ctx.state.inline_todo_ids
        }
        if non_inline:
            synth_prompt = (
                "Additional step results:\n"
                + "\n".join(f"- {tid}: {out}" for tid, out in non_inline.items())
                + "\n\nNow write the final user-facing response. "
                "Do not mention steps, plans, or internal process."
            )
        else:
            synth_prompt = "Write the final user-facing response."

        result = await streaming(
            ctx.deps.pulse_agent,
            ctx.deps.stream,
            user_prompt=synth_prompt,
            output_type=list[AgentMessage],
            deps=ctx.deps.agent_deps,
            toolsets=ctx.deps.toolsets,
            instructions=ctx.deps.instructions,
            message_history=ctx.state.main_history,
        )
        if isinstance(result.output, DeferredToolRequests):
            result = await resolve_deferred(
                ctx.deps.pulse_agent,
                result,
                ctx.deps.tentacle,
                ctx.deps.agent_deps,
                ctx.deps.toolsets,
                ctx.deps.stream,
            )
        logger.info("[%s] Synthesize complete", key)

        if ctx.deps.tentacle and key and ctx.state.card_ref:
            await ctx.deps.tentacle.feelers.todos.unpin_todo(key, ctx.state.card_ref)

        return End(cast(list[AgentMessage], result.output))


pulse_graph = Graph(nodes=[Triage, ExecuteStep, Synthesize])
