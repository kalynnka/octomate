"""Diagnose Bedrock prompt-cache hits across a Postgres-persisted conversation.

Runs two turns of the inkling agent through the real ConversationManager — so
turn 1's messages are written to Postgres and reloaded before turn 2 — then reports:

  * per-turn token usage, including cache_write / cache_read
  * whether the `new_messages() -> PG -> reload` round-trip is byte-faithful

A cache hit on turn 2 requires the resent prefix (tools + system + turn-1 messages)
to be byte-identical to what turn 1 sent. If the PG round-trip mutates anything
(dropped thinking signature, reordered tool-call args, lost fields, ...), the
prefix differs and Bedrock never cache-hits — so the fidelity diff below is the
first thing to look at.

Run with the same env / octomate.yaml the app uses (needs Postgres + Bedrock creds):

    .venv/bin/python scripts/cache_persistence_check.py
"""

# ruff: noqa: E402  — octomate imports run after the sys.path bootstrap below.
from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable so `python scripts/<file>.py` works from any cwd
# (the project isn't installed; main.py likewise relies on the repo root).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import difflib
import uuid
from typing import cast

from pydantic_ai import AgentRunResult
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from octomate.config import OctomateConfig
from octomate.managers.conversations import ConversationManager
from octomate.providers import ProviderRegistry
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.agent.inkling import build_inkling_agent

TURN_1 = "In about 120 words, explain how HTTP cookies work and why sites use them."
TURN_2 = "Now, in one sentence, what is the main privacy concern with them?"


def show_usage(label: str, result: AgentRunResult[object]) -> None:
    u = result.usage()
    print(
        f"{label}: input={u.input_tokens} output={u.output_tokens} "
        f"cache_write={u.cache_write_tokens} cache_read={u.cache_read_tokens}"
    )


def check_fidelity(
    in_memory: list[ModelMessage], from_pg: list[ModelMessage]
) -> bool:
    """Diff what the agent produced in memory against what came back from PG."""
    before = ModelMessagesTypeAdapter.dump_json(in_memory, indent=2).decode()
    after = ModelMessagesTypeAdapter.dump_json(from_pg, indent=2).decode()
    if before == after:
        print("fidelity: PG round-trip is byte-faithful ✓")
        return True
    print("fidelity: PG round-trip DIFFERS — this breaks the cache prefix:")
    diff = difflib.unified_diff(
        before.splitlines(), after.splitlines(), "in_memory", "from_pg", lineterm=""
    )
    print("\n".join(list(diff)[:80]))
    return False


async def main() -> None:
    # The arcanus transmuters (Conversation, ModelMessage, ...) must be blessed by
    # the active materia to touch their columns; the app enters this context in
    # Octomate.kick / the FastAPI lifespan. It's a sync context manager whose
    # contextvar persists across the awaits below.
    with sqlalchemy_materia():
        config = OctomateConfig()
        registry = ProviderRegistry(config.providers)
        model_config = config.agents.inkling.model
        model = registry.build_model(model_config)
        agent = build_inkling_agent(registry, model_config)
        print("model:", type(model).__name__, model.model_name)
        print("baked-in settings:", dict(model.settings or {}))
        print()

        cm = ConversationManager()
        key = ConversationKey(
            channel_tentacle_id="cache-test",
            chat_type="private",
            chat_id=f"cache-{uuid.uuid4().hex[:8]}",  # fresh conversation each run
            user_id="tester",
        )

        # --- Turn 1: writes history to Postgres ---
        conv = await cm.ensure(key, agent_tentacle_id="inkling")
        r1 = await agent.run(
            TURN_1,
            message_history=cast("list[ModelMessage]", list(conv.messages)) or None,
            conversation_id=str(conv.id),
        )
        await cm.record_agent_run(
            conv, run_id=r1.run_id, messages=r1.new_messages(), name="cache-test"
        )
        show_usage("turn 1", r1)

        # Cold-read turn 1 back from PG (fresh manager = no in-memory cache) and diff.
        reloaded = await ConversationManager().ensure(key, agent_tentacle_id="inkling")
        faithful = check_fidelity(list(r1.new_messages()), list(reloaded.messages))
        print()

        # --- Turn 2: resends the PG-reloaded history ---
        conv2 = await cm.ensure(key, agent_tentacle_id="inkling")
        r2 = await agent.run(
            TURN_2,
            message_history=cast("list[ModelMessage]", list(conv2.messages)) or None,
            conversation_id=str(conv2.id),
        )
        await cm.record_agent_run(
            conv2, run_id=r2.run_id, messages=r2.new_messages(), name="cache-test"
        )
        show_usage("turn 2", r2)
        print()

        if r2.usage().cache_read_tokens:
            print("RESULT: cache HIT on turn 2 ✓")
        elif not faithful:
            print(
                "RESULT: no cache read AND the PG round-trip mutated the messages "
                "(see diff above) — fix the persistence so reload == in-memory."
            )
        else:
            print(
                "RESULT: no cache read, but PG round-trip is faithful. Look elsewhere:\n"
                f"  - prefix size: turn-2 input was {r2.usage().input_tokens} tokens; Bedrock\n"
                "    needs the cached prefix (tools+system) >= ~1024 tokens to cache.\n"
                "  - thinking: adaptive reasoning may change the request shape per turn.\n"
                "  - tools/instructions must be identical across turns.\n"
                "  - confirm the model+region actually supports Converse prompt caching."
            )


if __name__ == "__main__":
    asyncio.run(main())
