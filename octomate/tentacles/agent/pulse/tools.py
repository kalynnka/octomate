from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Literal, cast

from langchain_community.agent_toolkits import FileManagementToolkit
from langchain_community.tools.shell.tool import ShellTool
from pydantic import BaseModel, Field
from pydantic_ai import RunContext, ToolDefinition
from pydantic_ai.ext.langchain import LangChainTool, LangChainToolset, tool_from_langchain
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from octomate.tentacles.agent.context import SessionContext
from octomate.transmuters.interactions import Todo

if TYPE_CHECKING:
    from octomate.tentacles.agent.pulse.state import PulseState, SubAgent

READONLY_FILE_TOOLS: frozenset[str] = frozenset(
    {"read_file", "list_directory", "file_search"}
)


def build_bash_tool() -> Tool[SessionContext]:
    return dataclasses.replace(tool_from_langchain(ShellTool()), requires_approval=True)


def build_file_management_toolset() -> AbstractToolset[Any]:
    toolset = LangChainToolset(cast(list[LangChainTool], FileManagementToolkit().get_tools()))

    def needs_approval(
        ctx: RunContext[Any],
        tool_def: ToolDefinition,
        args: dict[str, Any],
    ) -> bool:
        return tool_def.name not in READONLY_FILE_TOOLS

    return toolset.approval_required(needs_approval)


class TodoInput(BaseModel):
    todo_id: str
    title: str
    description: str = ""
    status: Literal["pending", "in_progress", "completed", "cancelled"] = "pending"
    active_form: str | None = None
    depends_on: list[str] = Field(default_factory=list)


def build_todo_list_toolset(state: PulseState) -> FunctionToolset[SessionContext]:
    local_to_server: dict[str, str] = {}
    prev_status: dict[str, str] = {}

    toolset: FunctionToolset[SessionContext] = FunctionToolset()

    @toolset.tool
    async def todo_list(ctx: RunContext[SessionContext], todos: list[TodoInput]) -> str:
        """Write or update the plan todo list. Call this whenever you make or revise a plan.

        Replace the entire list each call — include all todos, not just changed ones.
        Mark todos as in_progress when starting a step, and completed or cancelled when done.
        The list is shown to the user as a pinned card that updates in real time.
        """
        key = ctx.deps.session_key
        tentacle = ctx.deps.tentacle

        for item in todos:
            if item.todo_id not in local_to_server:
                if tentacle:
                    db_todo = await tentacle.feelers.todos.create_todo(
                        key, title=item.title, active_form=item.active_form
                    )
                    local_to_server[item.todo_id] = db_todo.todo_id or item.todo_id
                else:
                    local_to_server[item.todo_id] = item.todo_id
                prev_status[item.todo_id] = item.status
            elif prev_status.get(item.todo_id) != item.status:
                server_id = local_to_server[item.todo_id]
                if tentacle and server_id:
                    await tentacle.feelers.todos.update_todo(server_id, item.status)
                prev_status[item.todo_id] = item.status

        transmuters = [
            Todo(
                todo_id=local_to_server.get(t.todo_id, t.todo_id),
                title=t.title,
                description=t.description,
                status=t.status,
                active_form=t.active_form,
                depends_on=t.depends_on,
            )
            for t in todos
        ]
        state.todos = transmuters

        if tentacle:
            card_ref = await tentacle.feelers.todos.upsert_todo_list(
                key, transmuters, existing_ts=state.card_ref
            )
            if card_ref:
                if state.card_ref is None:
                    await tentacle.feelers.todos.pin_todo(key, card_ref)
                state.card_ref = card_ref

        counts: dict[str, int] = {}
        for t in todos:
            counts[t.status] = counts.get(t.status, 0) + 1
        parts = [f"{v} {k}" for k, v in counts.items() if v > 0]
        return f"wrote {len(todos)} todos ({', '.join(parts)})"

    return toolset


def build_delegate_toolset(
    subagents: dict[str, SubAgent],
    state: PulseState,
) -> FunctionToolset[SessionContext] | None:
    if not subagents:
        return None

    lines = [f'- "{sid}": {sub.description}' for sid, sub in subagents.items()]
    description = (
        "Delegate a task to a specialized subagent and receive its output as plain text.\n\n"
        "Subagents are focused executors — they complete one task and return a result.\n"
        "Pass relevant prior findings in 'context' so the subagent has the background it needs.\n\n"
        "Available subagents:\n" + "\n".join(lines)
    )

    toolset: FunctionToolset[SessionContext] = FunctionToolset()

    @toolset.tool(description=description)
    async def delegate(
        ctx: RunContext[SessionContext],
        subagent_tag: str,
        task_title: str,
        task_description: str,
        context: str = "",
    ) -> str:
        subagent = subagents.get(subagent_tag)
        if subagent is None:
            available = list(subagents)
            return f"unknown subagent: {subagent_tag!r}. Available: {available}"
        todo = Todo(todo_id="", title=task_title, description=task_description)
        return await subagent.execute(
            ctx.deps.session_key,
            todo,
            state,
            ctx.deps,
            stream=None,
            extra_context=context or None,
        )

    return toolset
