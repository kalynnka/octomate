"""PulseRunner — stepwise agent execution via Agent.iter().

Replaces single-shot ``Agent.run()`` with an iterative loop that observes each
graph node, chains tool calls, and short-circuits on early satisfactory
answers.  All intermediate reasoning stays invisible to the user — only the
final output (or escalation) is returned.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from pydantic_ai import Agent, AgentRunResult
from pydantic_ai._agent_graph import CallToolsNode, End, ModelRequestNode
from pydantic_ai.messages import ModelMessage
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import AbstractToolset

from octomate.agents.base import SessionContext
from octomate.schemas.actions import AgentMessage

if TYPE_CHECKING:
    from pydantic_ai.messages import UserContent

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 5


class PulseRunner:
    """Drives an Agent step-by-step using ``Agent.iter()``.

    *  Observes each node in the agent graph.
    *  Stops early when a satisfactory final result is produced.
    *  Caps the number of model request round-trips to ``max_steps``.
    *  Falls back to a direct ``Agent.run()`` with accumulated history
       if the iteration is cut short before a result is produced.
    """

    agent: Agent[SessionContext, list[AgentMessage] | DeferredToolRequests]
    max_steps: int

    def __init__(
        self,
        agent: Agent[SessionContext, list[AgentMessage] | DeferredToolRequests],
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> None:
        self.agent = agent
        self.max_steps = max_steps

    async def run(
        self,
        user_prompt: str | Sequence[UserContent],
        *,
        deps: SessionContext,
        toolsets: Sequence[AbstractToolset[SessionContext]] | None = None,
        instructions: str | None = None,
        message_history: Sequence[ModelMessage] | None = None,
    ) -> AgentRunResult[list[AgentMessage] | DeferredToolRequests]:
        """Execute the agent iteratively, returning the final result.

        Each ``ModelRequestNode`` counts as one step.  When ``max_steps`` is
        reached the loop stops iterating and falls back to a single
        ``Agent.run()`` with the conversation so far to obtain a clean final
        answer.
        """
        model_request_count = 0

        async with self.agent.iter(
            user_prompt,
            deps=deps,
            toolsets=toolsets or None,
            instructions=instructions,
            message_history=message_history,
        ) as agent_run:
            async for node in agent_run:
                if isinstance(node, ModelRequestNode):
                    model_request_count += 1
                    logger.debug(
                        "PulseRunner step %d/%d",
                        model_request_count,
                        self.max_steps,
                    )

                if isinstance(node, End):
                    break

                if isinstance(node, CallToolsNode):
                    if model_request_count >= self.max_steps:
                        logger.info(
                            "PulseRunner: reached max steps (%d), stopping",
                            self.max_steps,
                        )
                        break

        result = agent_run.result
        if result is not None:
            return result

        # Iteration was cut short (max_steps). Fall back to a direct run with
        # the conversation accumulated so far to produce a final answer.
        logger.debug("PulseRunner: falling back to direct run after step limit")
        return await self.agent.run(
            message_history=agent_run.all_messages(),
            deps=deps,
            toolsets=toolsets or None,
        )
