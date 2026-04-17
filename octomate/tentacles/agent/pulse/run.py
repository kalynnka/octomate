from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic_ai import Agent, AgentRunResult, DeferredToolResults
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import AbstractToolset

from octomate.tentacles.agent.context import SessionContext
from octomate.tentacles.agent.skills import SKILL_METADATA_KEY

if TYPE_CHECKING:
    from octomate.tentacles.channel.base import ChannelTentacle, StreamSink

from pydantic_ai.messages import PartEndEvent, ThinkingPart
from pydantic_ai.run import AgentRunResultEvent

from octomate.nerve import SummonAgent
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment

logger = logging.getLogger(__name__)
AgentOutputT = TypeVar("AgentOutputT")


async def streaming(
    agent: Agent[SessionContext, AgentOutputT],
    stream: StreamSink | None,
    tentacle: ChannelTentacle | None = None,
    **kwargs: Any,
) -> AgentRunResult[AgentOutputT]:
    if not stream:
        result = await agent.run(**kwargs)
    else:
        try:
            result = None
            async for event in agent.run_stream_events(**kwargs):
                if isinstance(event, AgentRunResultEvent):
                    result = event.result
                elif isinstance(event, PartEndEvent) and isinstance(
                    event.part, ThinkingPart
                ):
                    if event.part.content:
                        await stream.post_thinking_block(event.part.content)
            assert result is not None, "The stream run did not produce a result"
        except (AssertionError, NotImplementedError):
            result = await agent.run(**kwargs)

    if isinstance(result.output, DeferredToolRequests) and tentacle:
        result = await resolve_deferred(
            agent,
            result,
            tentacle,
            kwargs["deps"],
            toolsets=kwargs.get("toolsets"),
            stream=stream,
        )
    return result


async def resolve_deferred(
    agent: Agent[SessionContext, AgentOutputT],
    result: AgentRunResult[AgentOutputT],
    tentacle: ChannelTentacle,
    deps: SessionContext,
    toolsets: Sequence[AbstractToolset[SessionContext]] | None = None,
    stream: StreamSink | None = None,
) -> AgentRunResult[AgentOutputT]:
    """Resolve deferred tool calls (HITL interactions) until the agent produces output.

    Handles summon (dispatches via nerve, handover sends a notification to the
    channel), ask_user, and tool approval flows using the tentacle's feelers.
    """

    key = deps.session_key
    while isinstance(result.output, DeferredToolRequests):
        deferred = DeferredToolResults()

        for call in result.output.calls:
            if call.tool_name == "summon":
                args = call.args_as_dict()
                tag = args.get("tentacle_tag", "")
                summary = args.get("summary", "")
                agent_tentacle = tentacle.octopus.agent_tentacles.get(tag)
                if agent_tentacle is None:
                    deferred.calls[call.tool_call_id] = f"Unknown agent tentacle: {tag}"
                    continue

                content = MessageEvent(
                    tentacle_id=key.tentacle_id,
                    user_id=key.user_id,
                    chat_id=key.chat_id,
                    chat_type="group" if key.group_id else "private",
                    segments=[TextSegment(data={"text": summary})],
                )
                await tentacle.octopus.agent_nerve.send(
                    SummonAgent(
                        key=key,
                        agent_tag=tag,
                        contents=[content],
                        summary=summary,
                    )
                )

                if agent_tentacle.handover:
                    await tentacle.twitch(
                        key,
                        [
                            TextSegment(
                                data={
                                    "text": f"Tentacle *{tag}* has grabbed this thread 🐙!"
                                }
                            )
                        ],
                    )
                    deferred.calls[call.tool_call_id] = (
                        f"Thread handed over to agent '{tag}'. "
                        f"Do NOT produce any further response."
                    )
                else:
                    deferred.calls[call.tool_call_id] = (
                        f"Task dispatched to agent '{tag}'. "
                        f"It will reply in this thread when done."
                    )
            elif call.tool_name == "ask_user":
                args = call.args_as_dict()
                resp = await tentacle.feelers.questions.ask_question(
                    key,
                    args.get("question", ""),
                    session_key=key,
                    options=args.get("options"),
                )
                deferred.calls[call.tool_call_id] = (
                    resp.answer if resp else "(no response)"
                )

        for call in result.output.approvals:
            tool_meta = result.output.metadata.get(call.tool_call_id, {})

            thread = await tentacle.threads.get(key)
            if tentacle.feelers.confirm.is_session_allowed(
                str(thread.id), call.tool_name
            ):
                deferred.approvals[call.tool_call_id] = True
                continue

            action, future = await tentacle.feelers.confirm.create_confirmation(
                key=key,
                tool_name=call.tool_name,
                tool_call_id=call.tool_call_id,
                args=call.args_as_dict(),
                title=tool_meta.get("description", call.tool_name),
                description=tool_meta.get("description", ""),
                skill=tool_meta.get(SKILL_METADATA_KEY, ""),
                approvers=tool_meta.get("approvers"),
            )

            sent = await tentacle.feelers.confirm.send_confirmation(key, action)
            if not sent:
                await tentacle.feelers.confirm.expire_confirmation(
                    action.confirmation_id
                )
                deferred.approvals[call.tool_call_id] = False
                continue

            try:
                approved = await asyncio.wait_for(
                    future, timeout=tentacle.feelers.confirm.timeout
                )
            except TimeoutError:
                await tentacle.feelers.confirm.expire_confirmation(
                    action.confirmation_id
                )
                await tentacle.feelers.confirm.send_timeout_notification(key, action)
                approved = False
            except asyncio.CancelledError:
                await tentacle.feelers.confirm.expire_confirmation(
                    action.confirmation_id
                )
                await tentacle.feelers.confirm.send_timeout_notification(key, action)
                raise

            deferred.approvals[call.tool_call_id] = approved

        result = await streaming(
            agent,
            stream,
            message_history=result.all_messages(),
            deferred_tool_results=deferred,
            deps=deps,
            toolsets=toolsets,
        )

    return result
