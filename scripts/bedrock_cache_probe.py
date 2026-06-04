"""Probe why Bedrock prompt caching isn't engaging (cache_write stays 0).

Persistence is already proven faithful, and the cachePoint blocks are in the
request — yet Bedrock reports cache_write=0. This probe captures the actual
Converse request (to confirm the cachePoints + thinking config that reach the
wire) and runs the same single turn under three configs to isolate the cause:

  A. as configured            — adaptive reasoning on, cache TTLs '1h'/'5m'
  B. reasoning OFF            — isolates whether adaptive thinking blocks caching
  C. reasoning OFF + no TTL   — isolates whether the extended-cache `ttl` field
                                (vs a plain {type: default}) is the blocker

A single cold turn is enough: cache_write > 0 means caching *engaged*. Read the
output as: B>0 ⇒ thinking is the blocker; B=0,C>0 ⇒ the ttl field is; B=0,C=0 ⇒
neither, so it's prefix size or the model/region simply not caching.

    .venv/bin/python scripts/bedrock_cache_probe.py
"""

# ruff: noqa: E402  — octomate imports run after the sys.path bootstrap below.
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
from typing import Any

from octomate.config import OctomateConfig
from octomate.providers import ProviderRegistry
from octomate.tentacles.agent.inkling import build_inkling_agent

PROMPT = "In about 120 words, explain how HTTP cookies work and why sites use them."

captured: list[dict[str, Any]] = []


def on_request(params: dict[str, Any], **_: Any) -> None:
    # before-parameter-build fires for every bedrock-runtime op; keep Converse-ish ones.
    if isinstance(params, dict) and "messages" in params:
        captured.append(params)


def describe(params: dict[str, Any]) -> None:
    system = params.get("system") or []
    tools = (params.get("toolConfig") or {}).get("tools") or []
    sys_cp = [b["cachePoint"] for b in system if isinstance(b, dict) and "cachePoint" in b]
    tool_cp = [t["cachePoint"] for t in tools if isinstance(t, dict) and "cachePoint" in t]
    msg_cp = sum(
        1
        for m in (params.get("messages") or [])
        for b in (m.get("content") or [])
        if isinstance(b, dict) and "cachePoint" in b
    )
    print(
        f"    cachePoints -> system={len(sys_cp)} tools={len(tool_cp)} "
        f"messages={msg_cp} | example block: {sys_cp[0] if sys_cp else tool_cp[0] if tool_cp else None}"
    )
    print(f"    additionalModelRequestFields: {params.get('additionalModelRequestFields')}")
    print(f"    #tools={len(tools)} #system_blocks={len(system)} #messages={len(params.get('messages') or [])}")


def unwrap_client(model: Any) -> Any:
    # Defensive: skip any instrumentation wrappers to reach the boto3 client.
    while not hasattr(model, "client"):
        model = model.wrapped
    return model.client


async def run(agent: Any, label: str, **settings: Any) -> None:
    captured.clear()
    result = await agent.run(PROMPT, model_settings=settings or None)
    usage = result.usage()
    print(
        f"\n[{label}]\n  input={usage.input_tokens} output={usage.output_tokens} "
        f"cache_write={usage.cache_write_tokens} cache_read={usage.cache_read_tokens}"
    )
    if captured:
        describe(captured[-1])
    else:
        print("    (no Converse request captured — event hook missed)")


async def main() -> None:
    config = OctomateConfig()
    registry = ProviderRegistry(config.providers)
    agent = build_inkling_agent(registry, config.agents.inkling.model)

    client = unwrap_client(agent.model)
    client.meta.events.register("before-parameter-build.bedrock-runtime", on_request)

    await run(agent, "A: as configured (adaptive reasoning, ttl cache) — baseline ~1884")

    # Pinpoint opus-4-7's minimum cacheable prefix: pad the system prompt to push
    # the total across the threshold and watch where cache_write flips on. ~13
    # tokens per rep (measured), baseline ~1884.
    pad_unit = "This is additional operational background for the assistant. "
    reps = [0]

    @agent.system_prompt
    def _pad() -> str:
        return pad_unit * reps[0]

    cache_on = dict(
        thinking=False,
        bedrock_additional_model_requests_fields={},
        bedrock_cache_tool_definitions=True,
        bedrock_cache_instructions=True,
        bedrock_cache_messages=True,
    )
    for label, n in [
        ("B: input ~2.0k (just under 2048)", 10),
        ("C: input ~2.3k (just over 2048)", 30),
        ("D: input ~3.2k", 100),
        ("E: input ~8.4k (known to cache)", 500),
    ]:
        reps[0] = n
        await run(agent, label, **cache_on)


if __name__ == "__main__":
    asyncio.run(main())
